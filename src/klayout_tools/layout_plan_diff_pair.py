"""Width and strap parameters for differential-pair layout-plan groups."""

from __future__ import annotations

from typing import Any


def strap_splits_warning(group_id: str) -> str:
    return (
        f"device_groups (id '{group_id}'): derived params.strap_splits=true "
        "because the resolved splits is > 1 -- a diff_pair drawn without "
        "strapping leaves every sub-instance but one per device floating "
        "(issue #5557)"
    )


def apply_strap_splits(
    resolved: dict[str, Any],
    derived: dict[str, Any],
    group: dict[str, Any],
    overrides: dict[str, Any],
) -> list[str]:
    """Set ``strap_splits=true`` on a ``diff_pair`` group whose resolved
    ``splits`` is > 1 *and* whose resolved unit ``w_um`` can actually fit
    the strap (issue #5557); return any warning. The derived value is
    recorded in ``derived`` too, so a diverging explicit
    ``params.strap_splits`` override still gets the generic override-
    divergence ``warnings[]`` entry when the caller layers overrides on.

    ``splits`` resolves the same way the unit-``w_um`` division below does
    -- an explicit ``params.splits`` override or the generator default ``2``
    -- and a non-numeric/non-positive one derives nothing (the generator's
    own validator rejects it instead). The width check mirrors
    ``_diff_pair_validate``'s own floor (``STRAP_MIN_W_UM``: the four S/D
    strap tracks must fit the pads), applied to the *unit* width the plan
    will actually draw -- netlist-derived ``w_um`` divided by the resolved
    ``splits``, or the explicit override's value taken as already-unit. A
    plan whose width cannot fit the strap derives nothing and keeps
    today's unstrapped behaviour (sub-instance-level ports, k-th-device
    binding), never a rejected request the caller did not ask for; a
    caller who wants the strap regardless sets ``params.strap_splits``
    explicitly and hears about it from the generator's own validation."""
    if group["generator"] != "diff_pair":
        return []
    splits = overrides.get("splits", 2)
    is_valid = (
        isinstance(splits, (int, float)) and not isinstance(splits, bool) and splits > 1
    )
    if not is_valid:
        return []
    from .gen_diff_pair_strap import STRAP_MIN_W_UM

    # The two `w_um` sources are in different units: an explicit
    # `params.w_um` override is already the per-unit PCell param
    # (`apply_diff_pair_unit_w_um` deliberately leaves it alone), while the
    # netlist-derived value is the whole matched device's width and still
    # needs the same `splits` division the resolved param gets. Dividing the
    # override too understated the unit width by a factor of `splits`, so a
    # plan whose per-unit width fit the strap silently derived nothing --
    # #5557's floating-sub-instances failure mode.
    if "w_um" in overrides:
        unit_w = overrides["w_um"]
    else:
        whole_w = derived.get("w_um")
        unit_w = (
            whole_w / splits
            if isinstance(whole_w, (int, float)) and not isinstance(whole_w, bool)
            else None
        )
    if not (
        isinstance(unit_w, (int, float)) and not isinstance(unit_w, bool) and unit_w > 0
    ):
        return []
    if unit_w < STRAP_MIN_W_UM:
        return []
    derived["strap_splits"] = True
    resolved["strap_splits"] = True
    if "strap_splits" in overrides:
        return []
    return [strap_splits_warning(group["id"])]


def apply_diff_pair_unit_w_um(
    resolved: dict[str, Any],
    derived: dict[str, Any],
    group: dict[str, Any],
    overrides: dict[str, Any],
) -> list[str]:
    """Divide the netlist-derived ``w_um`` by the resolved ``splits`` for a
    ``diff_pair`` group (issue #1731's scope decision, moved here beside the
    other derived-param helpers). ``diff_pair``'s ``w_um`` PCell param is
    the *unit sub-instance* width, not the whole matched device's width --
    ``gen.py``'s ``_diff_pair_layout()`` draws each matched device as
    ``splits`` unit instances wide (default ``splits=2``).
    ``_netlist_derived_size_params()`` maps the digest's ``W`` straight onto
    ``w_um`` with no ``splits`` awareness, so a netlist device with ``W=20``
    would otherwise resolve ``w_um=20.0`` and draw a 40 um device -- 2x the
    schematic. Only the netlist-derived value is divided: an explicit
    ``params.w_um`` override is the caller directly setting the per-unit
    PCell param and is left alone. Returns any ``warnings[]`` entries."""
    if not (
        group["generator"] == "diff_pair"
        and "w_um" in derived
        and "w_um" not in overrides
    ):
        return []
    resolved_splits = overrides.get("splits", 2)
    is_valid_splits = (
        isinstance(resolved_splits, (int, float))
        and not isinstance(resolved_splits, bool)
        and resolved_splits > 0
    )
    if not is_valid_splits:
        # An invalid `splits` override (non-numeric, non-positive) is left
        # for `gen_describe.py`'s own `_diff_pair_validate` to reject
        # (`params.splits must be >= 1`) rather than this module dividing by
        # a value it cannot itself validate.
        return []
    netlist_w = derived["w_um"]
    unit_w = netlist_w / resolved_splits
    resolved["w_um"] = unit_w
    if resolved_splits == 1:
        return []
    return [
        f"device_groups (id '{group['id']}'): diff_pair draws each "
        f"matched device as 'splits' unit instances wide, so the "
        f"netlist-derived w_um ({netlist_w}) was auto-divided by "
        f"the resolved splits ({resolved_splits}) to w_um="
        f"{unit_w} so the drawn device reproduces the netlist W"
    ]
