"""``klt drc --engine both`` (issue #3973): run the curated and PDK-native DRC
engines against one layout and report where they differ.

This is a comparison/reporting surface over the two existing engines
(:func:`~klayout_tools.drc.run_drc`, :func:`~klayout_tools.drc.run_drc_klayout_engine`),
not a third verification engine. The two raw reports are embedded verbatim
and their own ``status`` / exit semantics are never rewritten; the
``comparison`` block is additive.

Comparison semantics follow ``docs/design/magic-oracle.md``: zero vs non-zero
plus *location*, never rule-id equality or marker counts (the two engines
report at different granularity). Only rules an explicit
:data:`RULE_MAPPING` group pairs up are comparable; every other rule that ran
on either side is listed as ``unmapped`` and can never produce an
``agree_*`` state. An absent or failed native run is ``not_checked`` -- never
a pass.
"""

from __future__ import annotations

from typing import Any

from .drc import (
    CODE_KLAYOUT_UNAVAILABLE,
    DrcError,
    drc_exit_code,
    run_drc,
    run_drc_klayout_engine,
)

#: Comparison states, worst-to-best precedence in :func:`_overall_state`.
STATE_AGREE_CLEAN = "agree_clean"
STATE_AGREE_VIOLATIONS = "agree_violations"
STATE_DISAGREEMENT = "disagreement"
STATE_NOT_COMPARABLE = "not_comparable"
STATE_NATIVE_UNAVAILABLE = "native_unavailable"
STATE_ENGINE_FAILED = "engine_failed"

#: Per-group states.
GROUP_AGREE_CLEAN = "agree_clean"
GROUP_AGREE_VIOLATIONS = "agree_violations"
GROUP_DISAGREEMENT = "disagreement"
GROUP_NOT_COVERED = "not_covered"

#: The deliberately-minimal rule mapping, keyed by curated deck name. Each
#: group pairs curated rule ids with the native RDB categories that check the
#: same thing in the same region; ``{"id", "curated_rules", "native_categories"}``.
#: It is empty on purpose: a wrong pairing manufactures false agreement or
#: false disagreement, so a group is added only with a documented, verified
#: equivalence (docs/cli/drc.md, "Cross-check"). With no group, every rule is
#: ``unmapped`` and the state is ``not_comparable`` -- the contract's honest
#: answer. ``run_drc_crosscheck(rule_mapping=...)`` accepts a caller mapping
#: for tests and for a verified project-local table.
RULE_MAPPING: dict[str, list[dict[str, Any]]] = {}


def _ids(report: dict[str, Any], key: str) -> set[str]:
    return set((report.get("coverage") or {}).get(key) or [])


def _bboxes(report: dict[str, Any], rules: set[str]) -> list[dict[str, int]]:
    return [v["bbox"] for v in report["violations"] if v["rule"] in rules]


def _touch(a: dict[str, int], b: dict[str, int]) -> bool:
    return (
        a["left"] <= b["right"]
        and b["left"] <= a["right"]
        and a["bottom"] <= b["top"]
        and b["bottom"] <= a["top"]
    )


def _unmatched(
    boxes: list[dict[str, int]], others: list[dict[str, int]]
) -> list[dict[str, int]]:
    return [box for box in boxes if not any(_touch(box, other) for other in others)]


def _check_identity(curated: dict[str, Any], native: dict[str, Any]) -> dict[str, Any]:
    """The shared identity of the two runs, or :class:`DrcError` if the
    embedded reports do not describe the same layout / PDK."""
    c_prov, n_prov = curated["provenance"], native["provenance"]
    c_in, n_in = c_prov.get("input"), n_prov.get("input")
    c_hash = (c_in or {}).get("content_hash")
    n_hash = (n_in or {}).get("content_hash")
    if c_hash != n_hash:
        raise DrcError(
            "cross-check refused: the curated and native runs hashed different "
            f"layouts ({c_hash} vs {n_hash}); the layout changed between runs"
        )
    c_pdk = (c_prov.get("pdk") or {}).get("name")
    n_pdk = (n_prov.get("pdk") or {}).get("name")
    if c_pdk != n_pdk:
        raise DrcError(
            "cross-check refused: the curated and native runs resolved "
            f"different PDK variants ({c_pdk!r} vs {n_pdk!r})"
        )
    return {
        "layout_content_hash": c_hash,
        "pdk": c_prov.get("pdk"),
        "klt_version": c_prov.get("klt_version"),
        "klayout_version": c_prov.get("klayout_version"),
        "curated_deck": c_prov.get("deck"),
        "native_deck": n_prov.get("deck"),
    }


def _compare_group(
    group: dict[str, Any], curated: dict[str, Any], native: dict[str, Any]
) -> dict[str, Any]:
    c_rules = set(group["curated_rules"])
    n_cats = set(group["native_categories"])
    uncovered = {
        "curated": sorted(c_rules - _ids(curated, "rules_checked")),
        "native": sorted(n_cats - _ids(native, "rule_categories")),
    }
    result: dict[str, Any] = {
        "id": group["id"],
        "curated_rules": sorted(c_rules),
        "native_categories": sorted(n_cats),
    }
    if uncovered["curated"] or uncovered["native"]:
        return {**result, "state": GROUP_NOT_COVERED, "uncovered": uncovered}
    c_boxes = _bboxes(curated, c_rules)
    n_boxes = _bboxes(native, n_cats)
    result["curated_markers"] = len(c_boxes)
    result["native_markers"] = len(n_boxes)
    if not c_boxes and not n_boxes:
        return {**result, "state": GROUP_AGREE_CLEAN}
    only_curated = _unmatched(c_boxes, n_boxes)
    only_native = _unmatched(n_boxes, c_boxes)
    if only_curated or only_native:
        return {
            **result,
            "state": GROUP_DISAGREEMENT,
            "only_curated": only_curated,
            "only_native": only_native,
        }
    return {**result, "state": GROUP_AGREE_VIOLATIONS}


def _overall_state(groups: list[dict[str, Any]], unmapped: dict[str, list[str]]) -> str:
    states = {g["state"] for g in groups}
    if GROUP_DISAGREEMENT in states:
        return STATE_DISAGREEMENT
    if (
        not groups
        or GROUP_NOT_COVERED in states
        or unmapped["curated"]
        or unmapped["native"]
    ):
        return STATE_NOT_COMPARABLE
    if GROUP_AGREE_VIOLATIONS in states:
        return STATE_AGREE_VIOLATIONS
    return STATE_AGREE_CLEAN


def compare_reports(
    curated: dict[str, Any],
    native: dict[str, Any],
    rule_mapping: list[dict[str, Any]],
) -> dict[str, Any]:
    """The ``comparison`` block for two finished reports on the same layout."""
    groups = [_compare_group(g, curated, native) for g in rule_mapping]
    mapped_curated = {r for g in rule_mapping for r in g["curated_rules"]}
    mapped_native = {c for g in rule_mapping for c in g["native_categories"]}
    unmapped = {
        "curated": sorted(_ids(curated, "rules_checked") - mapped_curated),
        "native": sorted(
            (_ids(native, "rule_categories") | _ids(native, "rules_checked"))
            - mapped_native
        ),
    }
    return {
        "state": _overall_state(groups, unmapped),
        "granularity": (
            "region: zero vs non-zero plus bbox overlap, "
            "never rule-id equality or counts"
        ),
        "groups": groups,
        "unmapped": unmapped,
    }


def _native_failure_state(exc: DrcError) -> str:
    """``native_unavailable`` for a missing prerequisite (binary, deck, host
    tools); ``engine_failed`` for any other native-side error."""
    if exc.code == CODE_KLAYOUT_UNAVAILABLE:
        return STATE_NATIVE_UNAVAILABLE
    message = str(exc)
    if "host command" in message or "host tool" in message:
        return STATE_NATIVE_UNAVAILABLE
    return STATE_ENGINE_FAILED


def run_drc_crosscheck(
    path: str,
    deck_name: str,
    native_deck_file: str | None,
    *,
    top: str | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    native_kwargs: dict[str, Any] | None = None,
    rule_mapping: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run both engines on ``path`` and return the cross-check envelope.

    A curated-side :class:`DrcError` (bad file, unknown deck) propagates: the
    layout cannot be checked at all. A native-side failure is *reported* as
    ``comparison.state`` ``native_unavailable`` / ``engine_failed`` with the
    native report ``null``.
    """
    curated = run_drc(
        path, deck_name, top=top, pdk_variant=pdk_variant, pdk_root=pdk_root
    )
    mapping = RULE_MAPPING.get(deck_name, []) if rule_mapping is None else rule_mapping

    native: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    if native_deck_file is None:
        failure = {
            "state": STATE_NATIVE_UNAVAILABLE,
            "message": "no PDK-native klayout DRC deck script found for this PDK "
            "variant (pass --deck-file)",
        }
    else:
        try:
            native = run_drc_klayout_engine(
                path,
                native_deck_file,
                top=top,
                pdk_variant=pdk_variant,
                pdk_root=pdk_root,
                deck_name=deck_name,
                **(native_kwargs or {}),
            )
        except DrcError as exc:
            failure = {"state": _native_failure_state(exc), "message": str(exc)}

    if native is None:
        assert failure is not None
        comparison: dict[str, Any] = {
            "state": failure["state"],
            "error": {"message": failure["message"]},
            "groups": [],
            "unmapped": {"curated": [], "native": []},
        }
        identity = None
    else:
        identity = _check_identity(curated, native)
        comparison = compare_reports(curated, native, mapping)

    return {
        "schema_version": 2,
        "mode": "crosscheck",
        "file": path,
        "status": _verdict_status(curated, native),
        "identity": identity,
        "comparison": comparison,
        "reports": {"curated": curated, "native": native},
    }


def _side_codes(curated: dict[str, Any], native: dict[str, Any] | None) -> list[int]:
    """Each side's own rollup exit code. A native run that did not complete
    is exit 4 (no verdict) -- never 0."""
    return [drc_exit_code(curated), 4 if native is None else drc_exit_code(native)]


def _verdict_status(curated: dict[str, Any], native: dict[str, Any] | None) -> str:
    """Envelope ``status``: ``violations`` if either side found some, else the
    first side's no-verdict token (``not_checked`` for a native run that never
    completed), else ``clean_partial``/``clean``. The embedded reports keep
    their own ``status`` untouched."""
    codes = _side_codes(curated, native)
    if 3 in codes:
        return "violations"
    if 4 in codes:
        for code, report in zip(codes, (curated, native), strict=True):
            if code == 4 and report is not None:
                return report["status"]
        return "not_checked"
    statuses = [curated["status"], native["status"]]  # type: ignore[index]
    return "clean_partial" if "clean_partial" in statuses else "clean"


def crosscheck_exit_code(report: dict[str, Any]) -> int:
    """Exit code of a cross-check envelope: 3 if either engine found
    violations, else 4 if either reached no verdict (including a native run
    that was unavailable or failed), else 0. Derived from each embedded
    report's own rollup code (:func:`~klayout_tools.drc.drc_exit_code`)."""
    codes = _side_codes(report["reports"]["curated"], report["reports"]["native"])
    return 3 if 3 in codes else 4 if 4 in codes else 0
