"""Validate rectangular ERC keepouts and measure conductor under the selected top."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._jsonnum import is_finite_number
from ._layout import clip_box, region
from ._paths import _reject_unknown_keys
from .coverage import work_id
from .erc_spec import ErcError

_RULE = "erc.role_keepout"


@dataclass(frozen=True)
class Keepout:
    box: tuple[float, float, float, float]
    roles: tuple[str, ...]


def _parse_box(value: Any, path: str) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(map(is_finite_number, value))
    ):
        raise ErcError(f"{path} must contain four finite micrometre coordinates")
    left, bottom, right, top = map(float, value)
    if left >= right or bottom >= top:
        raise ErcError(f"{path} requires left < right and bottom < top")
    return left, bottom, right, top


def parse_keepouts(
    spec: dict[str, Any], spec_path: str, stackup: list[str]
) -> list[Keepout] | None:
    if "keepouts" not in spec:
        return None
    raw = spec["keepouts"]
    if not isinstance(raw, list):
        raise ErcError(f"spec '{spec_path}': keepouts must be an array")
    result = []
    for index, entry in enumerate(raw):
        key = f"keepouts[{index}]"
        path = f"spec '{spec_path}': {key}"
        if not isinstance(entry, dict):
            raise ErcError(f"{path} must be an object")
        _reject_unknown_keys(entry, ("box", "roles", "below"), spec_path, key, ErcError)
        box = _parse_box(entry.get("box"), f"{path}.box")
        below = entry.get("below")
        if below not in stackup:
            raise ErcError(f"{path}.below must name a declared stackup role")
        roles = entry.get("roles")
        if not isinstance(roles, list):
            raise ErcError(f"{path}.roles must be an array of stackup names")
        selected: set[str] = set()
        for position, role in enumerate(roles):
            if role not in stackup or role in selected:
                raise ErcError(
                    f"{path}.roles[{position}] must name a distinct declared "
                    f"stackup role (got {role!r})"
                )
            selected.add(role)
        result.append(
            Keepout(
                box, tuple(r for r in stackup[: stackup.index(below)] if r in selected)
            )
        )
    return result


@dataclass
class KeepoutOutcome:
    findings: list[dict[str, Any]] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


def _window(
    kdb: Any, box: tuple[float, float, float, float], dbu: float, extent: Any
) -> Any:
    """Clip to the stream extent before converting to integer coordinates."""
    left, bottom, right, top = box
    for low, high in ((left, right), (bottom, top)):
        # At larger separations the two endpoints cannot round to one DBU.
        if high - low < 2 * dbu and round(low / dbu) == round(high / dbu):
            return None
    if extent.empty():
        return kdb.Region()
    bounds = (
        max(left, extent.left * dbu),
        max(bottom, extent.bottom * dbu),
        min(right, extent.right * dbu),
        min(top, extent.top * dbu),
    )
    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        return kdb.Region()
    return kdb.Region(clip_box(kdb, bounds, dbu))


def evaluate_keepouts(
    layout: Any,
    top: Any,
    role_layers: dict[str, tuple[int, int]],
    keepouts: list[Keepout] | None,
) -> KeepoutOutcome:
    import klayout.db as kdb

    from .erc import _bbox_dict, _finding

    outcome = KeepoutOutcome()
    if keepouts == []:
        outcome.skipped.append({"id": work_id(_RULE), "reason": "empty_keepouts"})
    regions: dict[str, Any] = {}
    for index, decl in enumerate(keepouts or ()):
        window = _window(kdb, decl.box, layout.dbu, top.bbox())
        if not decl.roles:
            outcome.skipped.append(
                {"id": work_id(_RULE, index), "reason": "no_roles_below_cutoff"}
            )
        for role in decl.roles:
            identity = work_id(_RULE, index, role)
            if window is None:
                outcome.skipped.append(
                    {"id": identity, "reason": "keepout_box_collapsed"}
                )
                continue
            if role not in regions:
                regions[role] = region(layout, top, role_layers[role]).merged()
            drawn = regions[role]
            if drawn.is_empty():
                outcome.skipped.append({"id": identity, "reason": "role_not_drawn"})
                continue
            outcome.checked.append(identity)
            overlap = drawn & window
            if not overlap.is_empty():
                layer, datatype = role_layers[role]
                finding = _finding(
                    _RULE,
                    f"keepouts[{index}] overlaps {role} conductor",
                    layer=f"{layer}/{datatype}",
                    bbox=_bbox_dict(overlap.bbox()),
                )
                finding.update(keepout_index=index, role=role)
                outcome.findings.append(finding)
    return outcome
