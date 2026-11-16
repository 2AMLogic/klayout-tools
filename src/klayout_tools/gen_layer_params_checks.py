"""Width-floor checks for ``mos_array`` layer-param resolution.

Split out of :mod:`klayout_tools.gen_layer_params` (file-size ratchet). The
helpers import their table lookups lazily because that module imports this
one at call time, not import time.
"""

from __future__ import annotations

from typing import Any


def resolve_mos_cap_mark(family: str, params: dict[str, Any], voltage_flavor_mark: Any):
    """MOS-capacitor marker ``LayerInfo`` (issue #3283), or ``None``.

    Raises ``GenError`` when the family declares no marker layer or ``w_um``
    is below the deck's COMP min width for the selected oxide.
    """
    if not params.get("mos_cap"):
        return None
    import klayout.db as kdb

    from .gen import GenError
    from .gen_layer_params import _PDK_MOS_CAP_MARK_LAYERS, _mos_cap_min_width_um

    pair = _PDK_MOS_CAP_MARK_LAYERS.get(family)
    if pair is None:
        raise GenError(
            f"generator 'mos_array': params.mos_cap is not supported on "
            f"PDK family '{family}' (no MOS-capacitor marker layer is "
            "declared for it)"
        )
    floor = _mos_cap_min_width_um(family, voltage_flavor_mark)
    if floor is not None and params["w_um"] < floor[0]:
        raise GenError(
            f"generator 'mos_array': params.w_um must be >= {floor[0]} for "
            f"a MOS capacitor on '{family}' (deck rule '{floor[1]}', the "
            "minimum COMP width for the selected oxide)"
        )
    return kdb.LayerInfo(*pair)


def check_dogbone_width(family: str, params: dict[str, Any]) -> None:
    """Issue #4851: with ``dogbone_terminals`` the PDK's own active min width
    is the ``w_um`` floor (below the generator-side enclosed-contact floor)."""
    if not params.get("dogbone_terminals") or params.get("mos_cap"):
        return
    from .gen import GenError
    from .gen_layer_params import _PDK_ROLE_LAYERS, _deck_min_width_um

    active_layer = _PDK_ROLE_LAYERS[family]["active"]
    active_floor = _deck_min_width_um(family, active_layer) if active_layer else None
    if active_floor is None:
        raise GenError(
            f"generator 'mos_array': params.dogbone_terminals is not "
            f"supported on PDK family '{family}' (its deck declares no "
            "active min-width rule)"
        )
    if params["w_um"] < active_floor[1]:
        raise GenError(
            f"generator 'mos_array': params.w_um must be >= "
            f"{active_floor[1]} with params.dogbone_terminals on "
            f"'{family}' (deck rule '{active_floor[0]}', the minimum "
            "active width)"
        )


def validate_mos_array_width(params: dict[str, Any]) -> None:
    """Generator-side ``w_um`` floor for ``mos_array`` (moved from
    ``gen_describe`` for the file-size ratchet)."""
    from .gen import GenError
    from .gen_describe import UNIT_MIN_W_UM

    if params["mos_cap"]:
        # The width floor of a MOS capacitor is the deck's own COMP min-width
        # rule for the selected oxide, resolved once the PDK is known (see
        # `_device_layer_params`); the flat generator-side floor below applies
        # to plain transistors only.
        if params["w_um"] <= 0:
            raise GenError("generator 'mos_array': params.w_um must be > 0")
        if not params["add_guard_ring"]:
            raise GenError(
                "generator 'mos_array': params.mos_cap requires "
                "params.add_guard_ring (the guard ring is the capacitor's "
                "body tie)"
            )
        if params["gate_contact"]:
            raise GenError(
                "generator 'mos_array': params.mos_cap does not support "
                "params.gate_contact"
            )
    elif params["dogbone_terminals"]:
        # Opt-in (issue #4851): the floor drops to the PDK's own active
        # min width, which needs the resolved family (see
        # `_device_layer_params`); here only positivity is checkable.
        if params["w_um"] <= 0:
            raise GenError("generator 'mos_array': params.w_um must be > 0")
        if params["finger_topology"] == "parallel" and params["fingers"] > 1:
            raise GenError(
                "generator 'mos_array': params.dogbone_terminals does not "
                "support params.finger_topology='parallel' with "
                "params.fingers > 1"
            )
    elif params["w_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'mos_array': params.w_um must be >= {UNIT_MIN_W_UM} "
            "(the smallest width that fits an enclosed contact with margin -- "
            "a generator-side structural floor, not the target PDK's own "
            "diffusion-width rule)"
        )
