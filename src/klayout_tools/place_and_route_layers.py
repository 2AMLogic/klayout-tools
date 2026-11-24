"""Signal-routing-layer reference table and range helpers for ``klt
place-and-route`` request validation (issues #2473, #4802).

Split out of ``place_and_route_request.py`` to keep that module under its
file-size ceiling; both names are re-exported from it (and from
:mod:`klayout_tools.place_and_route`) so existing imports resolve unchanged.
"""

from __future__ import annotations

from typing import Any

from .place_and_route_asap7 import RVT_LIBRARIES

#: Per-cell-library ``set_routing_layers -signal`` range for the ``"route"``
#: stage. Same not-derivable-from-the-install, verified-not-guessed posture
#: as :data:`_CTS_BUFFER_CELLS` -- but here ORFS *does* pin the value per
#: platform, as ``MIN_ROUTING_LAYER``/``MAX_ROUTING_LAYER``:
#:
#: - ``sky130_fd_sc_hd`` -> ``met1-met5``, from
#:   ``platforms/sky130hd/config.mk`` (``MIN_ROUTING_LAYER ?= met1``,
#:   ``MAX_ROUTING_LAYER ?= met5``).
#: - ``gf180mcu_fd_sc_mcu9t5v0`` -> ``Metal2-Metal5``, from
#:   ``platforms/gf180/config.mk`` (``MIN_ROUTING_LAYER ?= Metal2``,
#:   ``MAX_ROUTING_LAYER ?= Metal5``). This range deliberately starts one
#:   layer **above** the stack's bottom routing layer, unlike sky130hd's
#:   (issue #637): this library's standard cells pin out on ``Metal1``
#:   itself (``buf_4``'s ``I``/``Z`` are both ``LAYER Metal1`` in
#:   ``lef/gf180mcu_5LM_1TM_9K_9t_sc.lef``), so ``Metal1`` is left to pin
#:   access and intra-cell/power-rail geometry rather than opened up to
#:   free signal routing. sky130hd has no equivalent constraint -- its
#:   cells pin out on ``li1``, below ``met1`` entirely (``buf_4``'s
#:   ``A``/``X`` are ``LAYER li1`` in ``lef/sky130_fd_sc_hd_merged.lef``,
#:   with only the power rails on ``met1``). The layer-name case
#:   (``Metal``, not ``met``) is that PDK's own LEF convention, confirmed
#:   against the platform tech LEF: ``lef/gf180mcu_5LM_1TM_9K_9t_tech.lef``
#:   declares ``Metal1``..``Metal5`` as its five ``TYPE ROUTING`` layers.
#:
#: ORFS is **reference data only** for both tables, never a runtime
#: dependency -- nothing in this module shells out to, reads, or requires an
#: ORFS checkout, and no ORFS file is vendored here. In particular
#: ``platforms/gf180/config.mk``'s trailing ``-include
#: $(GF180_PRIVATE_DIR)/private.mk`` (a soft include, commented "for
#: proprietary tool enablements that are not public") is irrelevant to these
#: values: every variable read above is set in the public ``config.mk``
#: itself, above that line, and each is independently corroborated against
#: the platform's own open-source LEFs (issue #637). Values above read
#: 2026-08-09 from ``The-OpenROAD-Project/OpenROAD-flow-scripts`` @ ``master``.
#:
#: ``gf180mcu_fd_sc_mcu7t5v0`` (issue #1649) shares the ``gf180mcu_fd_sc_
#: mcu9t5v0`` entry's value verbatim: ``platforms/gf180/config.mk``'s
#: ``MIN_ROUTING_LAYER ?= Metal2``/``MAX_ROUTING_LAYER ?= Metal5`` are plain,
#: un-templated values -- unlike ``TECH_LEF``/``SC_LEF``/``FILL_CELLS``/etc,
#: they do not interpolate ``$(TRACK_OPTION)``, so the platform pins the same
#: routing-layer range regardless of the ``9t``/``7t`` track option. The same
#: ``Metal1``-reserved-for-pin-access rationale above also holds for this
#: library: its own ``buf_4``'s ``I``/``Z`` are both ``LAYER Metal1`` in
#: ``libs.ref/gf180mcu_fd_sc_mcu7t5v0/lef/gf180mcu_fd_sc_mcu7t5v0.lef``
#: (gf180mcuA variant).
#:
#: ``sg13g2_stdcell`` (issue #1784) -> ``Metal2-TopMetal2``. IHP ships no
#: ORFS platform config; the source of truth is its own LibreLane platform
#: config (``libs.tech/librelane/config.tcl``, IHP-Open-PDK v0.3.0):
#: ``RT_MIN_LAYER "Metal2"`` / ``RT_MAX_LAYER "TopMetal2"`` verbatim. Same
#: "``Metal1`` reserved for pin access" rationale as both gf180mcu entries
#: above: this library's own ``buf_4``'s ``A``/``X`` pins are both ``LAYER
#: Metal1`` in the installed ``libs.ref/sg13g2_stdcell/lef/sg13g2_stdcell.lef``.
#: The upper bound reaches all the way to ``TopMetal2`` -- this stack's own
#: ``libs.ref/sg13g2_stdcell/lef/sg13g2_tech.lef`` declares exactly seven
#: routing layers (``Metal1``..``Metal5``, ``TopMetal1``, ``TopMetal2``,
#: confirmed live via ``grep '^LAYER '``), matching that same LibreLane
#: config's own ``RT_MAX_LAYER``.
_ROUTING_LAYER_RANGE: dict[str, str] = {
    "sky130_fd_sc_hd": "met1-met5",
    "gf180mcu_fd_sc_mcu9t5v0": "Metal2-Metal5",
    "gf180mcu_fd_sc_mcu7t5v0": "Metal2-Metal5",
    "sg13g2_stdcell": "Metal2-TopMetal2",
}

#: Per-cell-library **routing-layer stack**, bottom-up -- every layer the
#: platform's own tech LEF declares ``TYPE ROUTING``, in stack order (issue
#: #2473). :data:`_ROUTING_LAYER_RANGE` above names only the two endpoints of
#: the *signal* range ``set_routing_layers -signal`` opens; this table is what
#: lets :func:`_signal_routing_layers` expand that range into the concrete
#: layer set, and therefore what lets :func:`_validate_io` reject an
#: ``io.layer_h``/``io.layer_v`` naming a real routing layer the router is
#: nevertheless never allowed to route signals on.
#:
#: Why that matters (issue #2473's own root cause): a top-level pin is only
#: reachable if the router can draw *conductor of that layer* up to it.
#: ``place_pins`` will happily place a port on a below-range layer, and
#: OpenROAD then writes the port rectangle to that layer's LEF ``PIN``/
#: ``LEFPIN`` purpose -- for sky130's ``li1`` that is GDS ``67/16``, not the
#: ``67/20`` conductor an extraction deck reads -- while the actual routing
#: reaches the cell pin from above through ``mcon``. The result is a DRC-clean
#: GDS whose top-level pins do not electrically exist in ``klt extract``'s
#: output, with `klt lvs` still reporting ``"match"`` because its structural
#: comparer does not need pins. Rejecting the request is two minutes of
#: routing cheaper than discovering that downstream.
#:
#: Same verified-not-guessed posture as every other reference table in this
#: module, one source per entry:
#:
#: - ``sky130_fd_sc_hd`` -> ``li1``, ``met1``..``met5``. Verified live
#:   against a real sky130A install (volare,
#:   ``libs.ref/sky130_fd_sc_hd/techlef/sky130_fd_sc_hd__nom.tlef``): its
#:   six ``TYPE ROUTING`` layers are ``li1``, ``met1``, ``met2``, ``met3``,
#:   ``met4``, ``met5``, declared in that order. ``li1`` sitting *below*
#:   this library's own ``MIN_ROUTING_LAYER ?= met1`` is exactly what
#:   :data:`_ROUTING_LAYER_RANGE`'s own docstring already records ("its
#:   cells pin out on ``li1``, below ``met1`` entirely").
#: - ``gf180mcu_fd_sc_mcu9t5v0``/``gf180mcu_fd_sc_mcu7t5v0`` -> ``Metal1``..
#:   ``Metal5``, from the same platform tech LEF
#:   (``lef/gf180mcu_5LM_1TM_9K_9t_tech.lef``) :data:`_ROUTING_LAYER_RANGE`
#:   already cites for declaring "``Metal1``..``Metal5`` as its five ``TYPE
#:   ROUTING`` layers", with ``Metal1`` deliberately left out of the signal
#:   range for pin access.
#: - ``sg13g2_stdcell`` -> ``Metal1``..``Metal5``, ``TopMetal1``,
#:   ``TopMetal2``, from the same ``libs.ref/sg13g2_stdcell/lef/
#:   sg13g2_tech.lef`` :data:`_ROUTING_LAYER_RANGE` already cites for
#:   declaring "exactly seven routing layers", again with ``Metal1``
#:   reserved for pin access.
#:
#: A ``cell_library`` absent from this table (or whose
#: :data:`_ROUTING_LAYER_RANGE` endpoints are not both in its stack) simply
#: skips the check -- an unverified library keeps the pre-#2473 behaviour
#: rather than being rejected on a guess.
_ROUTING_LAYER_STACK: dict[str, tuple[str, ...]] = {
    # lambdapdk v0.2.17, asap7/base/apr/asap7_tech.lef (TYPE ROUTING).
    **dict.fromkeys(RVT_LIBRARIES, tuple(f"M{i}" for i in range(1, 10))),
    "sky130_fd_sc_hd": ("li1", "met1", "met2", "met3", "met4", "met5"),
    "gf180mcu_fd_sc_mcu9t5v0": (
        "Metal1",
        "Metal2",
        "Metal3",
        "Metal4",
        "Metal5",
    ),
    "gf180mcu_fd_sc_mcu7t5v0": (
        "Metal1",
        "Metal2",
        "Metal3",
        "Metal4",
        "Metal5",
    ),
    "sg13g2_stdcell": (
        "Metal1",
        "Metal2",
        "Metal3",
        "Metal4",
        "Metal5",
        "TopMetal1",
        "TopMetal2",
    ),
}


def _signal_layer_range(
    cell_library: str, library: dict[str, Any] | None
) -> str | None:
    """The ``"<low>-<high>"`` range ``set_routing_layers -signal`` opens: a
    validated ``request.library.route_layers`` (issues #4127, #4802) if given,
    else the built-in per-library range, else ``None``."""

    if library is not None and "route_layers" in library:
        return library["route_layers"]
    return _ROUTING_LAYER_RANGE.get(cell_library)


def _signal_routing_layers(
    cell_library: str, library: dict[str, Any] | None = None
) -> tuple[str, ...] | None:
    """Expand :data:`_ROUTING_LAYER_RANGE`'s ``"<low>-<high>"`` endpoints for
    ``cell_library`` into the concrete, ordered set of layers
    ``set_routing_layers -signal`` actually opens for signal routing (issue
    #2473), using :data:`_ROUTING_LAYER_STACK` for the stack order.

    A ``request.library.route_layers`` cap overrides the built-in range (#4802).

    ``None`` -- meaning "no opinion, skip any check built on this" -- when
    either table has no entry for this library, or when the range's own
    endpoints are not both present in the stack (a table drift this function
    refuses to paper over by guessing). Never raises: this is a *narrowing*
    guard's input, and an unverified library must keep behaving exactly as it
    did before the guard existed.
    """

    stack = _ROUTING_LAYER_STACK.get(cell_library)
    layer_range = _signal_layer_range(cell_library, library)
    if stack is None or layer_range is None:
        return None
    low, _, high = layer_range.partition("-")
    if low not in stack or high not in stack:
        return None
    low_index = stack.index(low)
    high_index = stack.index(high)
    if low_index > high_index:
        return None
    return stack[low_index : high_index + 1]
