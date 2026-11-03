"""PDK seal-ring rule mode for ``klt ring-check --pdk`` (issue #4000).

Split out of :mod:`klayout_tools.ring_check` to keep that module under its
file-size ceiling; rule data lives in :mod:`klayout_tools.seal_ring_rules`.
"""

from __future__ import annotations

from typing import Any

from ._layout import load_layout
from .ring_check import RingCheckError, _assert_annulus, _select_top_cells

# ---------------------------------------------------------------------------
# PDK seal-ring mode (issue #4000)
# ---------------------------------------------------------------------------

#: ``check`` kind on every violation emitted by the PDK seal-ring mode.
SEAL_CHECK_KIND = "seal_ring_rule"


def run_seal_ring_check(
    layout_path: str,
    pdk: str,
    top: str | None = None,
) -> dict[str, Any]:
    """Check a drawn seal ring against ``pdk``'s seal-ring rule table.

    Returns the ``klt ring-check`` envelope (additive: ``mode: "pdk"``,
    ``pdk``, ``rules[]`` with per-rule ``status``, ``violations[]`` with
    ``bbox`` coordinates in database units). ``status`` is ``"broken"`` when
    any rule fails, ``"not_checked"`` when no rule could be evaluated (a PDK
    with no published rules -- never a pass), else ``"continuous"``. Rules the
    tool does not implement appear in ``rules[]`` as ``not_checked`` with a
    reason; they never vanish silently.
    """
    from .seal_ring_rules import PDK_SEAL_RULES, SUPPORTED_PDKS

    table = PDK_SEAL_RULES.get(pdk)
    if table is None:
        raise RingCheckError(
            f"unknown --pdk {pdk!r}: supported are {', '.join(SUPPORTED_PDKS)}"
        )

    layout = load_layout(layout_path, RingCheckError)
    import klayout.db as kdb

    top_cells = _select_top_cells(layout, top)
    dbu = layout.dbu

    rule_status: dict[str, str] = {}
    rule_info: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for rule in table.rules:
        entry: dict[str, Any] = {
            "id": rule.id,
            "description": rule.description,
            "source": {
                "file": rule.source_file,
                "tag": rule.source_tag,
                "kind": rule.source_kind,
            },
            "threshold_um": rule.threshold_um,
            "status": "not_checked",
            "reason": rule.not_checked_reason,
            "violation_count": 0,
        }
        rule_info.append(entry)
        if rule.check is None:
            continue
        found: list[dict[str, Any]] = []
        for cell in top_cells:
            found.extend(_SEAL_EVALUATORS[rule.check](kdb, layout, cell, rule, dbu))
        entry["status"] = "fail" if found else "pass"
        entry["reason"] = None
        entry["violation_count"] = len(found)
        violations.extend(found)
        rule_status[rule.id] = entry["status"]

    violations.sort(
        key=lambda v: (
            v["rule"],
            v["cell"],
            v["bbox"]["left"],
            v["bbox"]["bottom"],
            v["bbox"]["right"],
            v["bbox"]["top"],
        )
    )

    if violations:
        status = "broken"
    elif any(s == "pass" for s in rule_status.values()):
        status = "continuous"
    else:
        status = "not_checked"

    return {
        "schema_version": 1,
        "mode": "pdk",
        "file": layout_path,
        "pdk": pdk,
        "dbu_um": dbu,
        "status": status,
        "reason": table.not_checked_reason if status == "not_checked" else None,
        "violation_count": len(violations),
        "violations": violations,
        "rules": rule_info,
        "not_checked_rules": [
            r["id"] for r in rule_info if r["status"] == "not_checked"
        ],
    }


def _seal_layer(kdb: Any, layout: Any, cell: Any, lay: tuple[int, int]) -> Any:
    index = layout.find_layer(*lay)
    region = kdb.Region()
    if index is not None:
        region = kdb.Region(cell.begin_shapes_rec(index))
    region.merge()
    return region


def _seal_violation(
    rule: Any,
    cell: Any,
    layer: str,
    description: str,
    bbox: Any,
    polygon: Any | None = None,
) -> dict[str, Any]:
    points = None
    if polygon is not None:
        points = [[p.x, p.y] for p in polygon.each_point_hull()]
    return {
        "rule": rule.id,
        "description": description,
        "check": SEAL_CHECK_KIND,
        "kind": "rule_violation",
        "layer": layer,
        "cell": cell.name,
        "source": {"file": rule.source_file, "tag": rule.source_tag},
        "bbox": {
            "left": bbox.left,
            "bottom": bbox.bottom,
            "right": bbox.right,
            "top": bbox.top,
        },
        "polygon": points,
    }


def _label(lay: tuple[int, int]) -> str:
    return f"{lay[0]}/{lay[1]}"


def _seal_continuity(
    kdb: Any, layout: Any, cell: Any, rule: Any, dbu: float
) -> list[dict[str, Any]]:
    from .seal_ring_rules import SG13G2_EDGESEAL

    region = _seal_layer(kdb, layout, cell, SG13G2_EDGESEAL)
    out = []
    for v in _assert_annulus(
        kdb, region, cell.name, _label(SG13G2_EDGESEAL), ignore_enclosed=True
    ):
        v = dict(v)
        v.update(
            rule=rule.id,
            check=SEAL_CHECK_KIND,
            source={"file": rule.source_file, "tag": rule.source_tag},
        )
        out.append(v)
    return out


def _seal_passiv_enclosure(
    kdb: Any, layout: Any, cell: Any, rule: Any, dbu: float
) -> list[dict[str, Any]]:
    from .seal_ring_rules import SG13G2_EDGESEAL, SG13G2_PASSIV

    seal = _seal_layer(kdb, layout, cell, SG13G2_EDGESEAL)
    passiv = _seal_layer(kdb, layout, cell, SG13G2_PASSIV)
    enclosed = (passiv - seal).holes()
    bad = seal - enclosed
    bad.merge()
    return [
        _seal_violation(
            rule,
            cell,
            _label(SG13G2_EDGESEAL),
            "EdgeSeal is not enclosed by an unbroken Passiv ring",
            p.bbox(),
            p,
        )
        for p in bad.each()
    ]


def _seal_activ_space(
    kdb: Any, layout: Any, cell: Any, rule: Any, dbu: float
) -> list[dict[str, Any]]:
    from .seal_ring_rules import SG13G2_ACTIV, SG13G2_EDGESEAL, SG13G2_SEAL_B_LAYERS

    seal = _seal_layer(kdb, layout, cell, SG13G2_EDGESEAL)
    activ = _seal_layer(kdb, layout, cell, SG13G2_ACTIV)
    limit = round((rule.threshold_um or 0.0) / dbu)
    out = []
    for name, lay in SG13G2_SEAL_B_LAYERS:
        other = _seal_layer(kdb, layout, cell, lay) & seal
        for pair in activ.separation_check(
            other, limit, False, kdb.Metrics.Euclidian
        ).each():
            out.append(
                _seal_violation(
                    rule,
                    cell,
                    f"{_label(SG13G2_ACTIV)}~{_label(lay)}",
                    f"Seal.b_{name}: Activ space to EdgeSeal-{name} below "
                    f"{rule.threshold_um} um",
                    pair.bbox(),
                )
            )
    return out


def _seal_corner_length(
    kdb: Any, layout: Any, cell: Any, rule: Any, dbu: float
) -> list[dict[str, Any]]:
    from .seal_ring_rules import SG13G2_EDGESEAL

    edges = _seal_layer(kdb, layout, cell, SG13G2_EDGESEAL).edges()
    diag = edges.with_angle(45, False) + edges.with_angle(135, False)
    limit = round((rule.threshold_um or 0.0) / dbu)
    short = diag.with_length(0, limit, False)
    return [
        _seal_violation(
            rule,
            cell,
            _label(SG13G2_EDGESEAL),
            f"EdgeSeal 45-degree corner shorter than {rule.threshold_um} um",
            e.bbox(),
        )
        for e in short.each()
    ]


def _seal_single_ring(
    kdb: Any, layout: Any, cell: Any, rule: Any, dbu: float
) -> list[dict[str, Any]]:
    from .seal_ring_rules import SG13G2_EDGESEAL

    rings = [
        p
        for p in _seal_layer(kdb, layout, cell, SG13G2_EDGESEAL).each()
        if p.holes() > 0
    ]
    if len(rings) <= 1:
        return []
    return [
        _seal_violation(
            rule,
            cell,
            _label(SG13G2_EDGESEAL),
            f"{len(rings)} seal rings in one top cell; only one allowed",
            p.bbox(),
            p,
        )
        for p in rings
    ]


_SEAL_EVALUATORS = {
    "continuity": _seal_continuity,
    "passiv_enclosure": _seal_passiv_enclosure,
    "activ_space": _seal_activ_space,
    "corner_length": _seal_corner_length,
    "single_ring": _seal_single_ring,
}
