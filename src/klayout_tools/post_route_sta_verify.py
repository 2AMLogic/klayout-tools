"""``klt sta --check`` / ``--rerun``: verify a committed ``klt sta`` report
still reproduces from the request that produced it (issue #5841).

The ``klt sta`` counterpart of :func:`place_and_route.check_place_and_route_report`
and :func:`place_and_route.rerun_place_and_route_report` (issue #2224), built
from the same :mod:`klayout_tools._report_verify` blocks. It lives beside
:mod:`klayout_tools.post_route_sta` rather than in it so the 4.6k-line analysis
module does not grow.

- **Cheap mode** (:func:`check_sta_report`): re-resolve the request's geometry
  source (``def`` or ``verilog``) and standard-cell liberty(ies), re-hash them
  and compare against the digests the committed report recorded -- no OpenROAD
  run. A single-corner report records ``provenance.input.content_hash`` and
  ``provenance.deck.content_hash``; a multi-corner one (``pdk.corners`` or a
  map-form ``spef``) hoists ``provenance.input`` and carries one
  ``corners[].deck.content_hash`` per corner, so each corner is checked on its
  own and the drifted corner is named in the check's ``field``.
- **Full mode** (:func:`rerun_sta_report`): re-run ``run_sta`` and diff the
  verdict-bearing fields, excluding the version echoes
  (:data:`~._report_verify.VOLATILE_FLOW_PATHS`) and the run-scoped bookkeeping
  in :data:`RERUN_BOOKKEEPING_KEYS` / :data:`RERUN_BOOKKEEPING_PATHS`.

The request stays positional: a ``klt sta`` report does not echo the request
(see ``_report_verify.py``'s module docstring).
"""

from __future__ import annotations

import os
from typing import Any

from ._digest import content_hash
from ._report_verify import (
    VOLATILE_FLOW_PATHS,
    build_check_result,
    build_rerun_result,
    get_path,
    hash_check,
    load_committed_report,
    strip_keys,
    strip_path_patterns,
)
from ._report_verify_exclude import LIST_ITEM
from .post_route_sta import (
    PostRouteStaError,
    _resolve_def,
    _resolve_liberty,
    _resolve_verilog,
    _validate_corners,
    load_request,
    run_sta,
)

#: Run-scoped bookkeeping keys dropped from *both* sides of a ``--rerun`` diff,
#: at every depth. ``engine_log`` is the whole object -- ``invocation_id`` (a
#: fresh ``uuid4`` per OpenROAD invocation) and the retained log
#: ``directory``/``stdout_path``/``stderr_path``/``metadata_path`` named after
#: it -- so two identical runs never agree on it. Name-keyed, so it covers the
#: single-corner top-level ``engine_log`` and every ``corners[].engine_log``.
RERUN_BOOKKEEPING_KEYS: frozenset[str] = frozenset({"engine_log"})

#: Path-keyed exclusions: the ``sdf`` digest is a hash of bytes OpenSTA stamps
#: with a ``(DATE ...)`` header, so it differs between two runs of one request
#: (``docs/cli/sta.md``, "Post-route SDF per corner"). Only the digest is
#: dropped; ``sdf.path``/``scope`` are still compared.
RERUN_BOOKKEEPING_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("sdf", "content_hash"),
        ("corners", LIST_ITEM, "sdf", "content_hash"),
    }
)


def _canonicalize_sta_report_for_rerun_diff(report: dict[str, Any]) -> dict[str, Any]:
    """``report`` with the run-scoped bookkeeping removed -- the comparison
    view :func:`rerun_sta_report` diffs, never what it embeds as ``fresh``."""
    return strip_path_patterns(
        strip_keys(report, RERUN_BOOKKEEPING_KEYS), RERUN_BOOKKEEPING_PATHS
    )


def _sta_provenance_hashes(
    request_path: str,
    *,
    pdk_variant: str | None,
    pdk_root: str | None,
) -> tuple[str | None, dict[str, str | None], bool]:
    """Re-derive ``(input_content_hash, {resolved corner: deck_content_hash},
    multi_corner)`` for the request at ``request_path`` without invoking
    OpenROAD, mirroring :func:`post_route_sta.run_sta`'s own resolution.

    ``multi_corner`` is whether the report this request produces has the
    ``corners[]`` shape (``pdk.corners`` or a map-form ``spef``). Raises
    :class:`PostRouteStaError` for a request that cannot be read or resolved.
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))
    has_def = "def" in request
    has_verilog = "verilog" in request
    if has_def == has_verilog:
        raise PostRouteStaError(
            "request must include exactly one of 'def' or 'verilog'"
        )
    input_path = (
        _resolve_def(request["def"], request_dir)
        if has_def
        else _resolve_verilog(request["verilog"], request_dir)
    )

    pdk_spec = request["pdk"]
    if not isinstance(pdk_spec, dict):
        raise PostRouteStaError("request.pdk must be a JSON object")
    cell_library = pdk_spec.get("cell_library")
    if not isinstance(cell_library, str) or not cell_library:
        raise PostRouteStaError("request.pdk.cell_library is required")
    requested_corner = pdk_spec.get("corner")
    requested_corners = _validate_corners(pdk_spec.get("corners"))
    multi_corner = requested_corners is not None or isinstance(
        request.get("spef"), dict
    )
    names = requested_corners if requested_corners is not None else [requested_corner]

    deck_hashes: dict[str, str | None] = {}
    for name in names:
        liberty_path, corner, _info = _resolve_liberty(
            cell_library, name, variant=pdk_variant, root=pdk_root
        )
        deck_hashes[corner] = content_hash(liberty_path)
    return content_hash(input_path), deck_hashes, multi_corner


def check_sta_report(
    report_path: str,
    request_path: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """``klt sta <request> --check <report>`` (cheap mode, issue #5841):
    verify the committed report at ``report_path`` still reproduces from the
    request at ``request_path``, without invoking OpenROAD.

    Returns the shared ``--check`` payload
    (:func:`~._report_verify.build_check_result`): ``status: "match"`` when the
    geometry and every liberty hash agree with the recorded digests,
    ``"drifted"`` naming which moved otherwise. A recorded hash that is itself
    ``None`` (or a corner absent from the report) never counts as a match.

    Raises :class:`PostRouteStaError` for a missing/unparseable report or an
    unresolvable request -- never a traceback.
    """
    committed = load_committed_report(report_path, PostRouteStaError)
    input_hash, deck_hashes, multi_corner = _sta_provenance_hashes(
        request_path, pdk_variant=pdk_variant, pdk_root=pdk_root
    )
    checks = [
        hash_check(
            "provenance.input.content_hash",
            get_path(committed, ("provenance", "input", "content_hash")),
            input_hash,
        )
    ]
    if not multi_corner:
        (deck_hash,) = deck_hashes.values()
        checks.append(
            hash_check(
                "provenance.deck.content_hash",
                get_path(committed, ("provenance", "deck", "content_hash")),
                deck_hash,
            )
        )
        return build_check_result(report_path=report_path, checks=checks)

    entries = committed.get("corners")
    by_corner = {
        entry.get("corner"): entry
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict)
    }
    for corner, deck_hash in deck_hashes.items():
        checks.append(
            hash_check(
                f"corners[{corner}].deck.content_hash",
                get_path(by_corner.get(corner, {}), ("deck", "content_hash")),
                deck_hash,
            )
        )
    return build_check_result(report_path=report_path, checks=checks)


def rerun_sta_report(
    report_path: str,
    request_path: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """``klt sta <request> --check <report> --rerun`` (full mode, issue
    #5841): re-run the analysis the request declares and diff the fresh report
    against the committed one via :func:`~._report_verify.build_rerun_result`.

    Excludes :data:`~._report_verify.VOLATILE_FLOW_PATHS` (the klt/klayout/PDK
    version echoes plus ``engine_version``) and canonicalizes
    :data:`RERUN_BOOKKEEPING_KEYS` / :data:`RERUN_BOOKKEEPING_PATHS` out of
    both sides first. Both sides carry the same ``{path, scope}`` envelopes
    (schema 2), so a committed report is compared as-is, never re-scrubbed.

    **Re-running rewrites** ``.klt/sta/`` next to the request, exactly as a
    normal ``klt sta`` run does. Raises :class:`PostRouteStaError` for a
    missing/unparseable report or any error the re-run raises.
    """
    committed = load_committed_report(report_path, PostRouteStaError)
    fresh = run_sta(request_path, pdk_variant=pdk_variant, pdk_root=pdk_root)
    return build_rerun_result(
        report_path=report_path,
        committed=committed,
        fresh=fresh,
        exclude=VOLATILE_FLOW_PATHS,
        committed_for_diff=_canonicalize_sta_report_for_rerun_diff(committed),
        fresh_for_diff=_canonicalize_sta_report_for_rerun_diff(fresh),
    )
