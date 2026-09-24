"""Generate parametrized layout cells headlessly via KLayout PCells.

Pure library: :func:`generate` and :func:`list_generators` return plain
Python data (a ``dict`` of JSON-serialisable primitives) and never print,
mirroring ``render.py``/``sim.py``. Serialisation and human-readable
formatting live in the CLI command module (``cli/gen_cmd.py``).

This is phase 1 of Epic #152 (``klt gen``), the build carried by the accepted
spike, ``docs/design/layout-generator-spike.md`` -- read that document first;
its section 2 ("Proposed generator contract") settles the request/response
JSON shape this module implements, and section 1's "KLayout PCells (native)"
entry settles the implementation substrate: a generator is a
``pya.PCellDeclarationHelper`` subclass with a declared parameter schema and
a ``produce_impl()`` method, wrapped by a thin, generator-agnostic harness
(:func:`_produce`) that adapts the request/response envelope to it.

Scope (phase 1): **one** reference generator (``resistor_strip`` -- a row of
parametrized rectangles, no well/tap logic, not DRC-clean) that proves the
headless request -> geometry -> response loop end-to-end.

Scope (phase 2, this module's current state): the four analog primitive
families the spike scopes -- ``mos_array`` (matched transistor array),
``res_array`` (resistor/capacitor array), ``guard_ring`` (substrate/well tap
ring), and ``diff_pair`` (differential pair / current mirror cell, composing
``mos_array``'s unit-device drawing and ``guard_ring``'s ring drawing). Unlike
``resistor_strip``, these four generators are PDK-*aware*: they draw on each
resolved PDK family's own curated-DRC-deck layers (see
:data:`_PDK_ROLE_LAYERS`, sourced from ``klayout_tools.decks.sky130``/
``gf180mcu``, never a private layer map) and are sized to pass ``klt drc
--deck sky130``/``--deck gf180mcu`` clean on their documented default
``params``. Only the ``sky130``/``gf180mcu`` PDK families are supported by
these four generators (see :func:`_pdk_family`); ``resistor_strip`` remains
PDK-agnostic as it always was.

PDK resolution goes through the one resolver every other verb uses
(:func:`klayout_tools.pdk.find_pdk`) -- this module never implements its own
PDK lookup.

Deviation from the spike: the spike's example request shape carries a
``"pdk": {"name": ..., "variant": ...}`` pair where ``name`` is a PDK family
(e.g. ``"sky130"``) distinct from a specific install ``variant`` (e.g.
``"sky130A"``). ``klayout_tools.pdk.find_pdk`` has no family concept -- it
resolves a single ``variant`` string (the same one ``klt pdk find --pdk``
accepts) against an install root. This module keeps that one-resolver
contract rather than inventing a family/variant split the resolver doesn't
have: the request's ``pdk.variant`` field is passed straight through to
``find_pdk(variant=...)``, and the response's ``pdk.name``/``pdk.variant``
both echo the *resolved* variant (see :func:`generate`). A future phase may
revisit this once a real family-aware need surfaces.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._layout import write_layout
from ._provenance import build_provenance

# Issue #2347 moved every family's `validate`/`describe` pair (plus the
# helpers used only by them, and `_well_island_layer_params`, which #1875 had
# to leave behind because it calls `_well_island_isolation`) into
# `gen_describe.py` -- the third and last slice of this module's per-family
# split, after `gen_layer_params.py` (#1875) and the `gen_pcells/` package
# (#1698/#1713). `_GENERATOR_SPECS` below wires them up from there; nothing in
# `gen_describe.py` imports this module at load time, so the direction stays
# one-way.
from .gen_describe import (
    _bjt_array_describe,
    _bjt_array_validate,
    _bond_pad_describe,
    _bond_pad_validate,
    _cap_array_describe,
    _cap_array_validate,
    _diff_pair_describe,
    _diff_pair_validate,
    _esd_device_describe,
    _esd_device_validate,
    _guard_ring_describe,
    _guard_ring_validate,
    _mos_array_describe,
    _mos_array_validate,
    _res_array_describe,
    _res_array_validate,
    _resistor_strip_describe,
    _resistor_strip_validate,
    _well_island_describe,
    _well_island_layer_params,
    _well_island_validate,
)
from .gen_layer_params import (
    _CAP_GEOMETRY_MIN_KEYS as _CAP_GEOMETRY_MIN_KEYS,
)
from .gen_layer_params import (
    _DEFAULT_RES_FLAVOR as _DEFAULT_RES_FLAVOR,
)
from .gen_layer_params import (
    _GENERATOR_FAMILY_DEFERRED as _GENERATOR_FAMILY_DEFERRED,
)

# Issue #1875 moved the PDK layer-parameter/design-rule lookup subsystem (the
# per-family `_PDK_*` constant tables and the resolver functions that read
# them) into `gen_layer_params.py`. Everything below is re-imported here so
# `klayout_tools.gen.<name>` keeps working for every existing caller -- this
# module's own geometry-building code, `gen_pcells/*.py`'s lazy per-generator
# imports, `gen_compose_routing.py`/`layout_plan_execute.py`/`pdk_pcell.py`'s
# module-level imports, and `tests/test_gen.py`'s direct references -- none
# of which this split may break. The redundant `X as X` aliases mark the
# names this module's own code never calls directly as intentional
# re-exports (mirrors `gen_compose.py`'s own `_MAX_SAME_BLOCK_CROSS_LAYER_LANES
# as _MAX_SAME_BLOCK_CROSS_LAYER_LANES`-style re-exports from
# `gen_compose_routing.py`, issue #1708).
from .gen_layer_params import (
    _MAX_CAP_TOP_PLATE_REQUIRES,
    _MAX_RES_FLAVOR_LAYERS,
    CAP_BOTTOM_PLATE_MARGIN_UM,
    CAP_TOP_ESCAPE_CLEARANCE_UM,
    GUARD_RING_DEFAULT_CONTACTS_PER_SIDE,
    GUARD_RING_DEFAULT_PADDING_UM,
    GUARD_RING_DEFAULT_WIDTH_UM,
    SD_PAD_GATE_GAP_MIN_UM,
    _bjt_layer_params,
    _bond_pad_layer_params,
    _cap_array_layer_params,
    _diff_pair_layer_params,
    _esd_device_layer_params,
    _mos_array_layer_params,
    _resistor_layer_params,
    _resistor_strip_layer_params,
    _ring_layer_params,
)
from .gen_layer_params import (
    _MAX_METAL_RES_LEVEL as _MAX_METAL_RES_LEVEL,
)
from .gen_layer_params import (
    _METAL_RES_GEOMETRY_MIN_KEYS as _METAL_RES_GEOMETRY_MIN_KEYS,
)
from .gen_layer_params import (
    _MOS_ARRAY_WELL_TAP_FAMILIES as _MOS_ARRAY_WELL_TAP_FAMILIES,
)
from .gen_layer_params import (
    _MOS_ARRAY_WELL_TAP_NET_LABEL as _MOS_ARRAY_WELL_TAP_NET_LABEL,
)
from .gen_layer_params import (
    _PDK_CAP_GEOMETRY_MIN_UM as _PDK_CAP_GEOMETRY_MIN_UM,
)
from .gen_layer_params import (
    _PDK_CAP_TOP_PLATE_REQUIRES as _PDK_CAP_TOP_PLATE_REQUIRES,
)
from .gen_layer_params import (
    _PDK_CONTACT_GATE_EXTRA_OFFSET_UM as _PDK_CONTACT_GATE_EXTRA_OFFSET_UM,
)
from .gen_layer_params import (
    _PDK_GATE_BOTTOM_ENDCAP_UM as _PDK_GATE_BOTTOM_ENDCAP_UM,
)
from .gen_layer_params import (
    _PDK_GATE_PAD_ACTIVE_CLEARANCE_UM as _PDK_GATE_PAD_ACTIVE_CLEARANCE_UM,
)
from .gen_layer_params import (
    _PDK_METAL_RES_DEVICE_CLASS as _PDK_METAL_RES_DEVICE_CLASS,
)
from .gen_layer_params import (
    _PDK_METAL_RES_LEVEL_MIN_UM as _PDK_METAL_RES_LEVEL_MIN_UM,
)
from .gen_layer_params import (
    _PDK_METAL_RES_LEVELS as _PDK_METAL_RES_LEVELS,
)
from .gen_layer_params import (
    _PDK_RES_FLAVOR_DEVICE_CLASS as _PDK_RES_FLAVOR_DEVICE_CLASS,
)
from .gen_layer_params import (
    _PDK_RES_FLAVOR_LAYERS as _PDK_RES_FLAVOR_LAYERS,
)
from .gen_layer_params import (
    _PDK_RES_FLAVOR_MIN_W_UM as _PDK_RES_FLAVOR_MIN_W_UM,
)
from .gen_layer_params import (
    _PDK_ROLE_LAYERS as _PDK_ROLE_LAYERS,
)
from .gen_layer_params import (
    _PDK_SD_IMPLANT_MARGIN_UM as _PDK_SD_IMPLANT_MARGIN_UM,
)
from .gen_layer_params import (
    _PDK_VOLTAGE_FLAVOR_LAYERS as _PDK_VOLTAGE_FLAVOR_LAYERS,
)
from .gen_layer_params import (
    _PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM as _PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM,
)
from .gen_layer_params import (
    CONTACT_GAP_SAFE_UM as CONTACT_GAP_SAFE_UM,
)
from .gen_layer_params import (
    ESD_FINGER_WIDTH_MAX_UM as ESD_FINGER_WIDTH_MAX_UM,
)
from .gen_layer_params import (
    ESD_FINGER_WIDTH_MIN_UM as ESD_FINGER_WIDTH_MIN_UM,
)
from .gen_layer_params import (
    ESD_MAX_FINGERS_PER_RING as ESD_MAX_FINGERS_PER_RING,
)
from .gen_layer_params import (
    GATE_LENGTH_SAFE_MIN_UM as GATE_LENGTH_SAFE_MIN_UM,
)
from .gen_layer_params import (
    PAD_OPENING_GUIDELINE_MIN_UM as PAD_OPENING_GUIDELINE_MIN_UM,
)
from .gen_layer_params import (
    PAD_TOP_METAL_ENCLOSURE_MIN_UM as PAD_TOP_METAL_ENCLOSURE_MIN_UM,
)
from .gen_layer_params import (
    UNIT_MIN_W_UM as UNIT_MIN_W_UM,
)
from .gen_layer_params import (
    _cap_family_layers as _cap_family_layers,
)
from .gen_layer_params import (
    _cap_geometry_min_um as _cap_geometry_min_um,
)
from .gen_layer_params import (
    _cap_top_plate_requires as _cap_top_plate_requires,
)
from .gen_layer_params import (
    _contact_gate_extra_offset_um as _contact_gate_extra_offset_um,
)
from .gen_layer_params import (
    _device_layer_params as _device_layer_params,
)
from .gen_layer_params import (
    _gate_bottom_endcap_um as _gate_bottom_endcap_um,
)
from .gen_layer_params import (
    _gate_pad_clearance_um as _gate_pad_clearance_um,
)
from .gen_layer_params import (
    _metal_res_geometry_min_um as _metal_res_geometry_min_um,
)
from .gen_layer_params import (
    _metal_res_layers as _metal_res_layers,
)
from .gen_layer_params import (
    _mos_array_well_tap_role_layer as _mos_array_well_tap_role_layer,
)
from .gen_layer_params import (
    _pdk_family as _pdk_family,
)
from .gen_layer_params import (
    _reject_deferred_family as _reject_deferred_family,
)
from .gen_layer_params import (
    _res_flavor_layers as _res_flavor_layers,
)
from .gen_layer_params import (
    _res_flavor_min_width_um as _res_flavor_min_width_um,
)
from .gen_layer_params import (
    _res_flavor_min_width_um_floor as _res_flavor_min_width_um_floor,
)
from .gen_layer_params import (
    _resolve_expected_device_class as _resolve_expected_device_class,
)
from .gen_layer_params import (
    _ring_tap_implant_layer as _ring_tap_implant_layer,
)
from .gen_layer_params import (
    _role_layer_info as _role_layer_info,
)
from .gen_layer_params import (
    _sd_implant_margin_um as _sd_implant_margin_um,
)
from .gen_layer_params import (
    _voltage_flavor_mark_layer as _voltage_flavor_mark_layer,
)
from .gen_layer_params import (
    _voltage_flavor_mark_margin_um as _voltage_flavor_mark_margin_um,
)
from .gen_pcells import (
    _build_bjt_array_pcell,
    _build_bond_pad_pcell,
    _build_cap_array_pcell,
    _build_diff_pair_pcell,
    _build_esd_device_pcell,
    _build_guard_ring_pcell,
    _build_mos_array_pcell,
    _build_res_array_pcell,
    _build_resistor_strip_pcell,
    _build_well_island_pcell,
)
from .pdk import PdkNotFoundError, find_pdk, resolve_pdk_dbu

if TYPE_CHECKING:
    import klayout.db as kdb

#: Contract identifier for the request envelope (spike section 2).
REQUEST_SCHEMA = "klt.gen.request/1"

#: Bumped only on a non-additive (breaking) change to this command's own
#: response JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: Name this module registers its reference PCell library under. Registered
#: lazily, once per process (see :func:`_pcell_library`).
_PCELL_LIBRARY_NAME = "klt_gen_reference"

#: PCell parameters are never sourced from the request -- they carry no
#: caller-meaningful value (the drawing layer is a generator implementation
#: detail, not something a request should have to know KLayout's
#: ``LayerInfo`` shape to set). ``layer`` is ``resistor_strip``'s (phase 1)
#: single drawing-layer param; the rest are the phase-2 generators' per-role
#: layer params (see :func:`_device_layer_params` and friends) plus
#: ``well_present``, the harness-computed flag that tells a generator
#: whether its ``well_layer`` param is a real, DRC-checked layer for the
#: resolved PDK family. ``res_array``'s own per-flavour ``res_flavor_<i>_*``
#: mask slots are appended below :data:`_PDK_RES_FLAVOR_LAYERS`, since how
#: many of them exist is derived from that table; ``cap_array``'s own
#: ``cap_top_requires_<i>_*`` slots are appended the same way below
#: :data:`_PDK_CAP_TOP_PLATE_REQUIRES` (issue #1555).
_HIDDEN_PARAMS = {
    "layer",
    "active_layer",
    "poly_layer",
    "contact_layer",
    "metal_layer",
    "tap_layer",
    "well_layer",
    "well_present",
    "metal_label_layer",
    "metal_label_present",
    "well_tap_implant_layer",
    "well_tap_implant_present",
    "well_margin_resolved_um",
    "gate_pad_clearance_um",
    # `mos_array`/`diff_pair`/`esd_device`'s issue #1577 harness-computed
    # knobs -- resolved per PDK family exactly like `gate_pad_clearance_um`
    # above, never part of the request schema.
    "voltage_flavor_mark_margin_um",
    "contact_gate_offset_um",
    "bottom_endcap_um",
    "sd_implant_layer",
    "sd_implant_present",
    "sd_implant_margin_um",
    "bjt_mark_layer",
    "bjt_mark_present",
    "res_mark_layer",
    "res_mark_present",
    "dummy_layer",
    "dummy_present",
    "pad_layer",
    "top_metal_layer",
    "esd_mark_layer",
    "esd_mark_present",
    "salicide_block_layer",
    "salicide_block_present",
    "voltage_flavor_mark_layer",
    "voltage_flavor_mark_present",
    "cap_top_plate_layer",
    "cap_bottom_plate_layer",
    "cap_top_via_layer",
    "cap_top_via_present",
    "cap_top_via_metal_layer",
    "cap_top_via_metal_present",
    # `cap_array`'s per-family geometry floors (see
    # :data:`_PDK_CAP_GEOMETRY_MIN_UM`) -- harness-computed from the resolved
    # PDK family exactly like `gate_pad_clearance_um`/
    # `well_margin_resolved_um` above, never part of the request schema.
    # `cap_top_via_metal_min_w_um` (issue #1455) was omitted from this set
    # when it landed, so it leaked into `klt gen --list`'s `cap_array`
    # params despite being absent from `docs/cli/gen.md`'s own param table;
    # listing all four together here keeps the mechanism consistent (#1555).
    "cap_bottom_plate_margin_min_um",
    "cap_top_via_min_w_um",
    "cap_top_via_metal_min_w_um",
    "cap_min_spacing_um",
    # `guard_ring`/`mos_array`/`diff_pair`/`bjt_array`/`esd_device`'s own
    # tap/collector-ring implant (issue #1580, follow-up to #1577's
    # unit-device-only `sd_implant_*` fix) -- harness-computed exactly like
    # `sd_implant_layer`/`sd_implant_present` above, never part of the
    # request schema. See :func:`_ring_tap_implant_layer`.
    "ring_implant_layer",
    "ring_implant_present",
    # `res_array`'s own metal-layer-resistor geometry floors (issue #1639),
    # harness-computed from the resolved PDK family/`params.metal_level`
    # exactly like `cap_bottom_plate_margin_min_um`/friends above -- see
    # :data:`_PDK_METAL_RES_LEVEL_MIN_UM`. `params.metal_level` itself is
    # *not* hidden -- it is a real, request-facing param, exactly like
    # `flavor`.
    "metal_res_via_min_w_um",
    "metal_res_via_enclosure_min_um",
    "metal_res_via_space_min_um",
}

#: ``res_array``'s flavour mask slots are generated from
#: :data:`_MAX_RES_FLAVOR_LAYERS` (which depends on
#: :data:`klayout_tools.gen_layer_params._PDK_RES_FLAVOR_LAYERS`), so they
#: join :data:`_HIDDEN_PARAMS` here rather than being listed literally at its
#: own definition -- they are harness-computed layer params like every other
#: name in that set, never part of the request schema.
_HIDDEN_PARAMS |= {
    f"res_flavor_{i}_{suffix}"
    for i in range(_MAX_RES_FLAVOR_LAYERS)
    for suffix in ("layer", "present")
}

#: ``cap_array``'s top-plate requires-mask slots are generated from
#: :data:`_MAX_CAP_TOP_PLATE_REQUIRES` (which depends on
#: :data:`klayout_tools.gen_layer_params._PDK_CAP_TOP_PLATE_REQUIRES`), so --
#: exactly like ``res_array``'s own ``res_flavor_<i>_*`` slots -- they join
#: :data:`_HIDDEN_PARAMS` here rather than being listed literally at its own
#: definition.
_HIDDEN_PARAMS |= {
    f"cap_top_requires_{i}_{suffix}"
    for i in range(_MAX_CAP_TOP_PLATE_REQUIRES)
    for suffix in ("layer", "present")
}

#: Minimum contact/via drawn size (um) used by every phase-2 generator --
#: exceeds both curated decks' contact-size rule (sky130's ``licon1`` has no
#: size rule at all; gf180mcu's ``contact.width.1`` is 0.22um).
CONTACT_SIZE_UM = 0.22

#: The coarsest grid (um) any generator's geometry is placed on -- the
#: ``dbu=0.001`` fallback every ``_GENERATOR_SPECS`` entry below declares.
#: Used by :func:`_snap_square_box_um` to pre-round a contact's centre to
#: the manufacturing grid *before* building its box, so the later,
#: independent per-edge ``int(round(x / dbu))`` conversion in
#: :func:`_insert_boxes` can never round one edge up and the other down and
#: silently draw a contact 1 dbu narrower/shorter than ``CONTACT_SIZE_UM``
#: -- exactly the ``contact.width.1`` failure mode diagnosed in issue #685
#: (``CONTACT_SIZE_UM`` has zero DRC margin above gf180mcu's ``CO.1``
#: threshold, so a single dbu of rounding slop is enough to trip it).
#:
#: This stays a *fixed* 0.001 even though :func:`_produce` now resolves the
#: output layout's own dbu from the resolved PDK's tech LEF (issue #1496,
#: e.g. 0.0005 for gf180mcu's ``DATABASE MICRONS 2000``), and it is exactly
#: as safe there: a coordinate pre-snapped to a 0.001 grid is *exactly*
#: representable on any finer dbu that divides 0.001, so ``int(round())``
#: introduces no error at all rather than the sub-dbu slop #685 fixed. It is
#: deliberately not re-derived per-request from the resolved dbu -- doing so
#: would move geometry (a finer snap grid moves a contact's centre) on the
#: very PDKs this constant was validated against.
_GRID_DBU_UM = 0.001

#: Margin (um) an active/poly/tap region must keep around a contact it
#: encloses -- exceeds both curated decks' enclosure rules (sky130
#: ``diff.enclosing.licon.1``/``poly.enclosing.licon.1``: 0.04um/0.05um;
#: gf180mcu ``comp.enclosing.contact.1``/``poly2.enclosing.contact.1``:
#: 0.07um each).
ENCLOSURE_MARGIN_UM = 0.1

#: Minimum spacing (um) kept between same-layer shapes belonging to
#: different unit instances (adjacent array cells, ring segments) --
#: exceeds every curated same-layer spacing rule on both decks (sky130
#: ``li1.space.1``: 0.17um; gf180mcu ``comp.space.1``/``poly2.space.1``/
#: ``metal1.space.1``: 0.28um/0.24um/0.23um).
MIN_SAME_LAYER_SPACING_UM = 0.4

#: Margin (um) a well ring/tie keeps around the tap/comp region it encloses
#: -- exceeds gf180mcu's ``nwell.enclosing.comp.1`` (0.12um). sky130's
#: curated deck has no well-layer rule, so this only ever matters on
#: gf180mcu (see :data:`_PDK_ROLE_LAYERS`'s ``"well"`` entries).
WELL_ENCLOSURE_MARGIN_UM = 0.15

#: Minimum spacing (um) between two well regions held at **different**
#: potentials -- the rule ``well_island`` (issue #1421) enforces between its
#: own drawn well and every other well region the caller names in
#: ``params.isolate_from``. Measured *euclidian* (corner-to-corner, not
#: per-axis), matching the metric the source rule itself is written with.
#:
#: This is deliberately **not** the ``space``-rule value either curated DRC
#: deck checks today, because neither deck checks the different-potential
#: case at all:
#:
#: - ``sky130``: 1.27um, the SkyWater sign-off rule ``nwell.2`` ("Minimum
#:   spacing between N-well and N-well of different potential"), the rule the
#:   PDK's own KLayout deck codes as ``nwell.isolated(1.27, euclidian)``.
#:   This repo's curated sky130 DRC deck (``klayout_tools.decks.sky130``)
#:   transcribes **no** well-layer rule whatsoever -- see issue #1420 -- so
#:   ``klt drc --deck sky130`` cannot catch two same-flavour well islands
#:   that merged. That gap is precisely why the *generator* enforces the
#:   separation itself rather than deferring to the checker: it is the source
#:   of truth for the geometry it draws.
#: - ``gf180mcu``: 1.4um, DRM 7.4 Nwell rule ``NW.2b`` ("Min. Nwell Space
#:   (Outside DNWELL) [Different potential]", 3.3V column). This value is not
#:   invented here -- it is the *same* number ``klayout_tools.decks.gf180mcu``'s
#:   own ``nwell.space.1`` docstring already cites as the half of the
#:   ``NW.2a``/``NW.2b`` split it deliberately does not transcribe ("our
#:   engine has no connectivity/netlist information, so this uses the less
#:   strict ``NW.2a`` value"). ``klt gen`` *does* know the intended potential
#:   (the caller names it), so it can apply the strict half.
#:
#: A family absent from this table has no known different-potential well rule
#: to enforce; ``well_island`` reports that via ``drc_hints.notes`` rather
#: than silently applying another family's number (see
#: :func:`~klayout_tools.gen_describe._well_island_isolation`).
_PDK_WELL_ISOLATION_UM: dict[str, float] = {
    "sky130": 1.27,  # nwell.2 (different potential), euclidian
    "gf180mcu": 1.4,  # NW.2b (different potential), 3.3V
}


# --------------------------------------------------------------------------- #
# Shared, pure-Python (no `kdb`) geometry helpers -- each phase-2 generator's
# `produce_impl()` (draws, in dbu) and `_GeneratorSpec.describe()` (reports
# ports/device_count in um, for the JSON response) call the *same* one of
# these instead of independently re-deriving the same layout math, so the
# drawn GDS and the reported `ports[]` never drift apart.
# --------------------------------------------------------------------------- #


def _grid_snapped(dbu: float, *values_um: float) -> bool:
    """Whether any of ``values_um`` is not an exact multiple of ``dbu``."""

    def _snapped(value_um: float) -> bool:
        count = value_um / dbu
        return abs(count - round(count)) > 1e-9

    return any(_snapped(v) for v in values_um)


def _sd_pad_gate_offset_um(l_um: float, extra_offset_um: float = 0.0) -> float:
    """Extra clearance (um) :func:`_mos_finger_positions` inserts on *each*
    side of every gate stripe, between it and the adjacent S/D segment
    (issue #1187).

    ``0.0`` whenever ``l_um >= SD_PAD_GATE_GAP_MIN_UM`` -- true for every
    generator's own default (``GATE_LENGTH_SAFE_MIN_UM``, which is itself
    ``> SD_PAD_GATE_GAP_MIN_UM``) -- so existing callers at or above that
    default see byte-for-byte unchanged geometry. Below it, the offset grows
    just enough that the S/D-pad-to-pad gap across the gate
    (``l_um + 2 * offset``) reaches :data:`SD_PAD_GATE_GAP_MIN_UM` exactly,
    regardless of how small ``l_um`` itself is.

    ``extra_offset_um`` (issue #1577) is a second, independent floor: the
    resolved PDK family's :func:`_contact_gate_extra_offset_um`, needed on
    families whose real (not this repo's curated) deck checks a
    contact-to-gate spacing tighter than the small-``l_um`` makeup above
    alone guarantees. The two floors are independent asks -- a small ``l_um``
    already at the family's floor gets no *extra* padding on top, and a
    family with no floor at all (``0.0``, the default) sees exactly the
    pre-#1577 makeup-only behaviour -- so the return value is ``max``, not a
    sum, of the two.
    """
    makeup_um = max(0.0, (SD_PAD_GATE_GAP_MIN_UM - l_um) / 2.0)
    return max(makeup_um, extra_offset_um)


def _mos_finger_positions(
    l_um: float, fingers: int, extra_offset_um: float = 0.0
) -> tuple[list[tuple[float, float]], list[tuple[float, float]], float]:
    """``(seg_positions, poly_positions, total_len_um)`` for a ``fingers``-
    finger MOS unit device: ``fingers + 1`` contact-sized source/drain
    segments alternating with ``fingers`` ``l_um``-wide gate stripes, laid
    out left to right from ``x = 0``.

    Shared by both finger topologies (see :func:`_mos_unit_layout`) so the
    along-the-diffusion arithmetic -- and therefore ``total_len_um``, the
    array column pitch -- is identical whether or not the fingers are
    strapped in parallel.

    Each gate stripe sits :func:`_sd_pad_gate_offset_um` clear of the S/D
    segments either side of it (issue #1187) -- ``0.0`` for any ``l_um`` at
    or above :data:`SD_PAD_GATE_GAP_MIN_UM`, so the stripe still abuts the
    segments exactly as before every caller's own default. Below that
    threshold, the offset pads the unit device's *pitch* (``total_len_um``
    grows) rather than the gate itself (``l_um`` is drawn exactly as
    requested), so the S/D local-metal pads :func:`_mos_unit_layout` draws
    flush to ``seg_positions`` stay a legal same-layer-metal-spacing gap
    apart even at a PDK's absolute minimum gate length.

    ``extra_offset_um`` (issue #1577) is forwarded to
    :func:`_sd_pad_gate_offset_um` unchanged -- see that function's own
    docstring for why it is a second, independent floor rather than added to
    the small-``l_um`` makeup.
    """
    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    pad_offset_um = _sd_pad_gate_offset_um(l_um, extra_offset_um)
    seg_positions: list[tuple[float, float]] = []
    poly_positions: list[tuple[float, float]] = []
    x = 0.0
    for i in range(fingers + 1):
        seg_positions.append((x, x + contact_region_um))
        x += contact_region_um
        if i < fingers:
            x += pad_offset_um
            poly_positions.append((x, x + l_um))
            x += l_um + pad_offset_um
    return seg_positions, poly_positions, x


def _mos_unit_layout(
    w_um: float,
    l_um: float,
    fingers: int,
    gate_contact: bool = False,
    finger_topology: str = "series",
    gate_pad_clearance_um: float = 0.0,
    contact_gate_offset_um: float = 0.0,
    bottom_endcap_um: float = 0.0,
    sd_implant_margin_um: float = 0.0,
) -> dict[str, Any]:
    """One MOS-like unit device: a diffusion strip crossed by ``fingers``
    poly gates, with a contact + local-metal pad in each source/drain
    segment (``fingers + 1`` of them) between/around the gates.

    ``finger_topology`` selects what ``fingers > 1`` *means* electrically
    (issue #777). ``"parallel"`` -- what "an N-finger device" conventionally
    denotes -- straps the alternating S/D segments and ties every gate, so
    the unit is one device of width ``fingers * w_um``; that geometry is
    built by :func:`_mos_unit_strapped_layout`. ``"series"`` (this
    function's own body, and the default so
    :func:`_diff_pair_layout`/:func:`_esd_device_layout` keep their existing
    output) draws the bare fingers with no straps at all, which extracts as
    ``fingers`` transistors chained source-to-drain on ``fingers``
    independent gate nets.

    In the ``"series"`` shape with ``fingers == 1`` there is nothing but the
    two end segments and the one gate, so the unit reports plain
    ``s_xy``/``d_xy``/``g_xy`` exactly as before. For ``fingers > 1`` (issue
    #781, the follow-up to #777's option 1) *every* finger is padded and
    *every* terminal is exposed: ``seg_xy`` holds all ``fingers + 1`` S/D
    segment centres (west to east) and ``gate_xy`` holds all ``fingers`` gate
    pad centres, so :func:`_mos_array_describe` can report a ``U<i>_S<j>``/
    ``U<i>_D<j>`` port for every segment and a ``U<i>_G<j>`` port for every
    gate instead of leaving the interior of the chain undrivable. ``s_xy``/
    ``d_xy``/``g_xy`` still alias ``seg_xy[0]``/``seg_xy[-1]``/``gate_xy[0]``
    so single-finger callers (:func:`_diff_pair_layout`,
    :func:`_esd_device_layout`, and the ``fingers == 1`` case here) need no
    change.

    Every gate finger carries the same **poly landing pad** that extends
    past the diffusion's top edge, so a contact can be placed on the gate
    outside the channel (issue #461). Without it the gate poly shared both
    the top and bottom edge of the diffusion, leaving nowhere legal for a
    gate contact to land: one at the diff edge straddles it
    (``poly.enclosing.licon.1``/``diff.enclosing.licon.1`` violations) and
    one moved inward sits over the channel (a gate-oxide short). The pad is a
    ``contact_region_um`` square (``CONTACT_SIZE_UM`` enclosed by
    ``ENCLOSURE_MARGIN_UM`` on every side -- the same enclosure budget the
    S/D contacts use), centred on the gate ``ENCLOSURE_MARGIN_UM`` clear of
    the diffusion, and each reported gate port sits at its own pad's centre.
    Adjacent pads end up exactly ``l_um + 2 * `` :func:`_sd_pad_gate_offset_um`
    ``(l_um)`` apart -- i.e. never closer than :data:`SD_PAD_GATE_GAP_MIN_UM`
    (issue #1187), the same guarantee the S/D local-metal pads either side of
    a gate now have -- so padding every finger (not just the first) costs no
    new DRC risk, regardless of how small ``l_um`` is.

    ``gate_contact`` (issue #492) finishes that stack: it draws a contact
    **and** a local-metal pad on the landing pad, so the gate terminal is a
    metal-role pad exactly like S/D and `klt gen-compose`'s router can reach
    it with no hand-drawn licon/li1 patchwork. It also *moves* the landing
    pad's contact region up by :data:`MIN_SAME_LAYER_SPACING_UM`: the #461
    pad abuts the diffusion's top edge, so a ``contact_region_um`` metal
    square centred on it would share an edge with -- and therefore merge
    into, i.e. **short** to -- the S/D local-metal pads that fill the
    diffusion's own height. The poly grows into a stem of the same
    ``contact_region_um`` width across that clearance so the poly stays one
    connected region, and the reported gate port moves to the raised
    contact's centre. Left at ``False`` (the default) the drawn geometry and
    the reported gate port are byte-for-byte the pre-#492 bare-poly gate.

    ``gate_pad_clearance_um`` (issue #1450, resolved per PDK family by
    :func:`_gate_pad_clearance_um`) stands that landing pad off the
    diffusion's top edge instead of abutting it, bridging the gap with a poly
    stem of exactly ``l_um`` -- the gate stripe's own width, so the poly stays
    one connected region *and* the stem contributes no new edge at
    ``y == w_um`` for a poly-to-active separation check to measure. That
    matters because the pad is wider than the gate stripe for any realistic
    ``l_um``: with no clearance, the two overhangs' undersides sit flush on
    the diffusion's own top edge, facing unrelated ``active`` at zero
    distance. sky130/gf180mcu pass ``0.0`` (neither curated deck checks
    poly-to-unrelated-active spacing at all, and lifting the pad would move
    their long-pinned ``U<i>_G`` port), so their drawn geometry is
    byte-for-byte unchanged; sg13g2 passes a real clearance. The stem is only
    ever ``l_um`` wide, never the pad's width, so a caller's existing gate
    length still fully determines poly width here -- no new minimum-width or
    poly-space rule binds. Only the ``"series"`` shape needs this: the
    ``"parallel"`` strapped shape (:func:`_mos_unit_strapped_layout`) already
    runs every gate stripe a full :data:`MIN_SAME_LAYER_SPACING_UM` plus a
    contact region past the diffusion before widening into its comb.

    Three more knobs, all issue #1577, all resolved per PDK family
    (``0.0`` -- byte-for-byte unchanged geometry -- on every family besides
    ``gf180mcu``, whose real signoff deck needs each): ``contact_gate_offset_um``
    (:func:`_contact_gate_extra_offset_um`) is forwarded to
    :func:`_mos_finger_positions` as a second, independent floor on top of
    the small-``l_um`` makeup :func:`_sd_pad_gate_offset_um` already applies;
    ``bottom_endcap_um`` (:func:`_gate_bottom_endcap_um`) extends every gate
    stripe's *bottom* edge (``y == 0``) down past the diffusion, mirroring
    the endcap the *top* edge's own #461 landing pad already provides there;
    ``sd_implant_margin_um`` (:func:`_sd_implant_margin_um`) draws a
    ``"sd_implant"`` box -- present only when this margin is positive --
    covering the unit's own ``active`` box grown by that margin on every
    side, so `produce_impl` can lay a source/drain implant mask over the
    device body (see :func:`_device_layer_params`'s ``sd_implant_layer``/
    ``sd_implant_present``, resolved off ``params.flavor``).
    """
    if finger_topology == "parallel" and fingers > 1:
        return _mos_unit_strapped_layout(
            w_um,
            l_um,
            fingers,
            gate_contact,
            contact_gate_offset_um,
            bottom_endcap_um,
            sd_implant_margin_um,
        )

    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    seg_positions, poly_positions, total_len_um = _mos_finger_positions(
        l_um, fingers, contact_gate_offset_um
    )

    boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "active": [(0.0, 0.0, total_len_um, w_um)],
        "poly": [(px0, -bottom_endcap_um, px1, w_um) for (px0, px1) in poly_positions],
        "contact": [],
        "metal": [],
        "sd_implant": (
            [
                (
                    -sd_implant_margin_um,
                    -sd_implant_margin_um,
                    total_len_um + sd_implant_margin_um,
                    w_um + sd_implant_margin_um,
                )
            ]
            if sd_implant_margin_um > 0
            else []
        ),
    }
    contact_half = CONTACT_SIZE_UM / 2.0
    seg_xy: list[tuple[float, float]] = []
    for sx0, sx1 in seg_positions:
        boxes["metal"].append((sx0, 0.0, sx1, w_um))
        cx = (sx0 + sx1) / 2.0
        cy = w_um / 2.0
        boxes["contact"].append(
            (cx - contact_half, cy - contact_half, cx + contact_half, cy + contact_half)
        )
        seg_xy.append((cx, cy))

    s_xy = seg_xy[0]
    d_xy = seg_xy[-1]

    gate_xy: list[tuple[float, float]] = []
    if poly_positions:
        # Gate landing pad above *every* finger (issue #781 -- previously
        # only the first). Each pad's half-width (contact_region_um / 2) can
        # exceed l_um / 2, so it overhangs its narrow gate on both sides --
        # that is the point: the pad, not the sub-contact-width gate, is
        # what encloses the contact. It abuts its own gate at y == w_um
        # (keeping that gate's poly one connected region) and extends
        # contact_region_um past it. Adjacent pads end up exactly
        # l_um + 2 * _sd_pad_gate_offset_um(l_um) apart -- never closer than
        # SD_PAD_GATE_GAP_MIN_UM (issue #1187), the same guarantee the S/D
        # local-metal pads either side of a gate now have, so padding every
        # finger binds no new spacing rule regardless of how small l_um is.
        #
        # With `gate_contact` each pad's contact region is first pushed
        # MIN_SAME_LAYER_SPACING_UM further out (the poly simply grows into a
        # stem of the same width across that clearance, so it stays one
        # connected region): the metal pad drawn on it is a full
        # contact_region_um square, and centred on the #461 pad it would
        # share the y == w_um edge with the S/D local-metal pads either side
        # -- merging into one polygon and shorting the gate to source/drain.
        #
        # `gate_pad_clearance_um` (issue #1450) additionally lifts the whole
        # pad (and, with `gate_contact`, its stem/contact/metal stack) that
        # far off y == w_um on families whose curated deck checks
        # poly-to-unrelated-active spacing -- see the function docstring. The
        # gap is bridged by a poly box of the gate stripe's *own* width
        # (px0..px1), never the pad's: a full-width bridge would just move the
        # overhang's underside edge up by the clearance instead of removing it
        # from the diffusion's edge, and a stripe-width one adds no edge at
        # y == w_um at all (it is flush with the gate stripe below it).
        pad_half = contact_region_um / 2.0
        clearance_um = max(gate_pad_clearance_um, 0.0)
        stem_um = MIN_SAME_LAYER_SPACING_UM if gate_contact else 0.0
        pad_y0 = w_um + clearance_um
        gate_ext_um = clearance_um + stem_um + contact_region_um
        g_cy = pad_y0 + stem_um + contact_region_um / 2.0
        for px0, px1 in poly_positions:
            g_cx = (px0 + px1) / 2.0
            if clearance_um:
                boxes["poly"].append((px0, w_um, px1, pad_y0))
            boxes["poly"].append(
                (g_cx - pad_half, pad_y0, g_cx + pad_half, w_um + gate_ext_um)
            )
            if gate_contact:
                boxes["contact"].append(
                    (
                        g_cx - contact_half,
                        g_cy - contact_half,
                        g_cx + contact_half,
                        g_cy + contact_half,
                    )
                )
                boxes["metal"].append(
                    (g_cx - pad_half, g_cy - pad_half, g_cx + pad_half, g_cy + pad_half)
                )
            gate_xy.append((g_cx, g_cy))
        g_xy = gate_xy[0]
        g_width_um = contact_region_um
    else:
        g_xy = (total_len_um / 2.0, w_um)
        g_width_um = l_um
        gate_ext_um = 0.0

    return {
        "total_len_um": total_len_um,
        "height_um": w_um,
        # Full drawn height including the gate landing pad *and* (issue
        # #1577) the gate stripe's own bottom-edge endcap extension -- what
        # row-to-row placement (see `_mos_array_layout`/`_diff_pair_layout`)
        # and the enclosing well box must span. Without folding
        # `bottom_endcap_um` in here too, row-to-row placement would still
        # only reserve `MIN_SAME_LAYER_SPACING_UM` between each row's *own*
        # y == 0 origin and the row above's landing pad -- but the actual
        # drawn poly now reaches `bottom_endcap_um` *below* that origin, so
        # the real gap to the previous row's poly would shrink by exactly
        # that amount, tripping gf180mcu's real `PL.3a` ("Space on
        # COMP/Field", 0.24um) same-layer poly spacing rule between the two
        # rows' unrelated gate stripes. `height_um` stays the bare diffusion
        # height because it is the S/D ports' perpendicular width.
        "bbox_height_um": w_um + gate_ext_um + bottom_endcap_um,
        "gate_ext_um": gate_ext_um,
        "boxes_um": boxes,
        "s_xy": s_xy,
        "d_xy": d_xy,
        "g_xy": g_xy,
        "g_width_um": g_width_um,
        # Every S/D segment centre (fingers + 1 of them, west to east) and
        # every gate pad centre (fingers of them) -- issue #781. `s_xy`/
        # `d_xy`/`g_xy` above alias `seg_xy[0]`/`seg_xy[-1]`/`gate_xy[0]`.
        # `gate_xy` is `[]` only when `poly_positions` is (never happens for
        # `fingers >= 1`, since `_mos_finger_positions` always yields at
        # least one gate).
        "seg_xy": seg_xy,
        "gate_xy": gate_xy,
        # Perpendicular width of the reported S/D ports. Equal to the bare
        # diffusion height here (the ports sit on the S/D pads, which span
        # it); the strapped topology reports its strap-rail height instead
        # (see `_mos_unit_strapped_layout`).
        "sd_width_um": w_um,
        "finger_topology": "series",
    }


def _mos_unit_strapped_layout(
    w_um: float,
    l_um: float,
    fingers: int,
    gate_contact: bool = False,
    contact_gate_offset_um: float = 0.0,
    bottom_endcap_um: float = 0.0,
    sd_implant_margin_um: float = 0.0,
) -> dict[str, Any]:
    """One **parallel** multi-finger MOS unit device (issue #777): the same
    ``fingers``-stripe diffusion :func:`_mos_unit_layout` draws, plus the
    straps that actually make it one device of width ``fingers * w_um``.

    Conventionally "an N-finger device" means N gate stripes over a shared
    diffusion with the alternating S/D segments strapped together and all N
    gates tied -- the whole reason to fold a wide device rather than place N
    separate unit devices. Drawing only the stripes (what
    :func:`_mos_unit_layout`'s ``"series"`` shape does) instead yields N
    transistors chained source-to-drain on N floating gates, which is not a
    device any caller asked for and cannot be repaired from outside: the
    interior segments and gates have no reported ports and no landing pads.

    The strapping is two full-width ``metal`` rails plus a ``poly`` comb,
    drawn bottom to top on the single role pair the phase-2 generators have.
    There is no second routing metal to cross on, so the two S/D rails go on
    *opposite* sides of the diffusion and the gate stripes cross **under**
    the drain rail on ``poly`` to reach their comb -- legal on both curated
    decks, and no contact there means no connection::

        y = 0                    source rail  (metal, full width)
        + MIN_SAME_LAYER_SPACING_UM           source stubs (even segments)
        + w_um                   diffusion + per-segment S/D pads
        + MIN_SAME_LAYER_SPACING_UM           drain stubs (odd segments)
        + CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
                                 drain rail   (metal, full width)
        + MIN_SAME_LAYER_SPACING_UM           gate stripes crossing under it
        + CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
                                 gate comb    (poly, ties every finger)

    Even-indexed S/D segments (0, 2, ...) drop a stub to the source rail and
    odd-indexed ones (1, 3, ...) raise a stub to the drain rail, so every
    finger sits between the two rails -- i.e. ``fingers`` transistors in
    parallel on one gate net, which ``klt lvs``'s
    ``options.combine_devices`` folds into the single ``W = fingers * w_um``
    device the schematic states. Each gate stripe is simply extended past
    the drain rail into the comb (so the whole gate net stays one connected
    poly region, no per-finger landing pad needed), and the reported gate
    terminal (with
    ``gate_contact``, a contact + local-metal pad; without it, bare poly, as
    ``mos_array``'s pre-#492 default) sits at the comb's centre, a full
    :data:`MIN_SAME_LAYER_SPACING_UM` clear of the drain rail's metal.

    Every clearance is :data:`MIN_SAME_LAYER_SPACING_UM`, which already
    exceeds both curated decks' same-layer spacing rules, and every rail and
    stub is :data:`CONTACT_SIZE_UM` + 2 * :data:`ENCLOSURE_MARGIN_UM` wide
    -- the same width budget the S/D pads use -- so no minimum-width rule
    binds either. ``total_len_um`` is identical to the series shape's, so
    the array column pitch does not move; only ``bbox_height_um`` grows.

    ``contact_gate_offset_um``/``bottom_endcap_um``/``sd_implant_margin_um``
    (issue #1577) mirror :func:`_mos_unit_layout`'s own three knobs of the
    same name exactly -- see that function's docstring. The gate stripe's
    bottom edge here is ``diff_y0`` (coincident with the diffusion's own
    bottom edge, the same flush-endcap gap the bare "series" shape's
    ``y == 0`` edge has), so ``bottom_endcap_um`` extends it downward from
    there instead of from ``0.0``.
    """
    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    clearance_um = MIN_SAME_LAYER_SPACING_UM
    seg_positions, poly_positions, total_len_um = _mos_finger_positions(
        l_um, fingers, contact_gate_offset_um
    )

    source_rail_y1 = contact_region_um
    diff_y0 = source_rail_y1 + clearance_um
    diff_y1 = diff_y0 + w_um
    drain_rail_y0 = diff_y1 + clearance_um
    drain_rail_y1 = drain_rail_y0 + contact_region_um
    comb_y0 = drain_rail_y1 + clearance_um
    comb_y1 = comb_y0 + contact_region_um

    boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "active": [(0.0, diff_y0, total_len_um, diff_y1)],
        # Each gate stripe runs the diffusion height and then up past the
        # drain rail into the comb, keeping the whole gate net one connected
        # poly region.
        "poly": [
            (px0, diff_y0 - bottom_endcap_um, px1, comb_y0)
            for (px0, px1) in poly_positions
        ],
        "contact": [],
        "metal": [
            (0.0, 0.0, total_len_um, source_rail_y1),
            (0.0, drain_rail_y0, total_len_um, drain_rail_y1),
        ],
        "sd_implant": (
            [
                (
                    -sd_implant_margin_um,
                    diff_y0 - sd_implant_margin_um,
                    total_len_um + sd_implant_margin_um,
                    diff_y1 + sd_implant_margin_um,
                )
            ]
            if sd_implant_margin_um > 0
            else []
        ),
    }
    boxes["poly"].append(
        (poly_positions[0][0], comb_y0, poly_positions[-1][1], comb_y1)
    )

    contact_half = CONTACT_SIZE_UM / 2.0
    for i, (sx0, sx1) in enumerate(seg_positions):
        boxes["metal"].append((sx0, diff_y0, sx1, diff_y1))
        cx = (sx0 + sx1) / 2.0
        cy = (diff_y0 + diff_y1) / 2.0
        boxes["contact"].append(
            (cx - contact_half, cy - contact_half, cx + contact_half, cy + contact_half)
        )
        if i % 2 == 0:
            boxes["metal"].append((sx0, source_rail_y1, sx1, diff_y0))
        else:
            boxes["metal"].append((sx0, diff_y1, sx1, drain_rail_y0))

    # S/D terminals are reported on their rails, at the end the series shape
    # reported them at (west for source, east for drain) so a consumer's
    # `direction_deg`-driven stub still exits the cell the same way.
    s_xy = (contact_region_um / 2.0, source_rail_y1 / 2.0)
    d_xy = (
        total_len_um - contact_region_um / 2.0,
        (drain_rail_y0 + drain_rail_y1) / 2.0,
    )

    g_cx = (poly_positions[0][0] + poly_positions[-1][1]) / 2.0
    g_cy = (comb_y0 + comb_y1) / 2.0
    if gate_contact:
        pad_half = contact_region_um / 2.0
        boxes["contact"].append(
            (
                g_cx - contact_half,
                g_cy - contact_half,
                g_cx + contact_half,
                g_cy + contact_half,
            )
        )
        boxes["metal"].append(
            (g_cx - pad_half, g_cy - pad_half, g_cx + pad_half, g_cy + pad_half)
        )

    return {
        "total_len_um": total_len_um,
        "height_um": w_um,
        "bbox_height_um": comb_y1,
        "gate_ext_um": comb_y1 - diff_y1,
        "boxes_um": boxes,
        "s_xy": s_xy,
        "d_xy": d_xy,
        "g_xy": (g_cx, g_cy),
        "g_width_um": contact_region_um,
        # The S/D ports sit on the strap rails, not on the diffusion pads,
        # so their perpendicular width is the rail height.
        "sd_width_um": contact_region_um,
        "finger_topology": "parallel",
    }


def _centroid_order(rows: int, cols: int) -> list[tuple[int, int]]:
    """A centroid-symmetric visiting order over a ``rows`` x ``cols`` grid:
    positions are visited nearest-to-center first, each immediately followed
    by its point-reflection through the grid center -- the numbering
    convention ``mos_array``'s ``topology="common_centroid"`` uses so a
    downstream matching/LVS consumer can pair instance ``2k`` with
    ``2k + 1`` as centroid-symmetric partners (see ``docs/cli/gen.md``).
    """
    positions = [(r, c) for r in range(rows) for c in range(cols)]
    cy = (rows - 1) / 2.0
    cx = (cols - 1) / 2.0

    def _dist(pos: tuple[int, int]) -> float:
        r, c = pos
        return (r - cy) ** 2 + (c - cx) ** 2

    positions.sort(key=lambda pos: (_dist(pos), pos[0], pos[1]))

    ordered: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for pos in positions:
        if pos in seen:
            continue
        ordered.append(pos)
        seen.add(pos)
        mirror = (rows - 1 - pos[0], cols - 1 - pos[1])
        if mirror != pos and mirror not in seen:
            ordered.append(mirror)
            seen.add(mirror)
    return ordered


def _mos_array_well_tap_layout(
    min_x0_um: float, min_y0_um: float, max_x1_um: float, max_y1_um: float
) -> dict[str, Any]:
    """Well-tie tap pad for ``mos_array``'s ``flavor='pfet'`` well, on a PDK
    family whose curated deck derives a well tie from an implant-covered
    ``"active"`` shape inside the well rather than a distinct tap mask (issue
    #1473 -- see the ``"well_tap_implant"`` role in :data:`_PDK_ROLE_LAYERS`,
    e.g. ``sg13cmos5l``'s ``nSD``/``EXTRACTION_DECK.tap_nplus``).

    One pad ties the *entire* shared well: :func:`_mos_array_layout`'s own
    ``well_box_um`` already merges every unit device's well enclosure into a
    single shape, and the curated deck's own tie derivation
    (``tap_nplus_region & active & nwell``) connects that single ``nwell``
    conductor -- and therefore every PMOS body terminal sitting in it -- to
    whatever this one pad's contact/metal reach. So this is drawn once per
    array, never once per unit device, mirroring the "one net biases the
    whole tub" fact the derivation itself relies on.

    Placed :data:`MIN_SAME_LAYER_SPACING_UM` clear of the array's own active
    bounding box (``(min_x0_um, min_y0_um, max_x1_um, max_y1_um)``, the same
    box :func:`_mos_array_layout` margins into ``well_box_um``), on the
    *same* shared ``"active"`` role every unit device's own source/drain
    diffusion uses -- comfortably inside a curated deck's own same-layer
    spacing floor (e.g. sg13cmos5l's ``activ.space.1``, 0.21um), so the
    differently-implanted tap pad never collides with a real unit device's
    diffusion under DRC."""
    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    x0 = max_x1_um + MIN_SAME_LAYER_SPACING_UM
    y0 = min_y0_um
    x1 = x0 + contact_region_um
    y1 = y0 + contact_region_um
    contact_half = CONTACT_SIZE_UM / 2.0
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    return {
        "active": (x0, y0, x1, y1),
        "contact": (
            cx - contact_half,
            cy - contact_half,
            cx + contact_half,
            cy + contact_half,
        ),
        "metal": (x0, y0, x1, y1),
        "width_um": contact_region_um,
        "xy": (cx, cy),
    }


def _mos_array_layout(
    w_um: float,
    l_um: float,
    fingers: int,
    rows: int,
    cols: int,
    dummy: int,
    topology: str,
    gate_contact: bool = False,
    finger_topology: str = "parallel",
    gate_pad_clearance_um: float = 0.0,
    draw_well_tap: bool = False,
    add_guard_ring: bool = False,
    ring_gap_side: str = "",
    ring_gap_um: float = 0.0,
    ring_gap_offset_um: float = 0.0,
    ring_padding_um: float = GUARD_RING_DEFAULT_PADDING_UM,
    contact_gate_offset_um: float = 0.0,
    bottom_endcap_um: float = 0.0,
    sd_implant_margin_um: float = 0.0,
    voltage_flavor_mark_margin_um: float = WELL_ENCLOSURE_MARGIN_UM,
    interior_channel_um: float = 0.0,
) -> dict[str, Any]:
    """A ``rows`` x ``cols`` grid of :func:`_mos_unit_layout` unit devices,
    with ``dummy`` extra unit-device columns flanking each side.

    ``finger_topology`` is forwarded to :func:`_mos_unit_layout` and decides
    whether a ``fingers > 1`` unit is one parallel folded device (the
    default) or an unstrapped series chain (issue #777).
    ``gate_pad_clearance_um`` (issue #1450) is likewise forwarded; it grows
    each unit's ``bbox_height_um``, so the row pitch and the enclosing well
    follow it without any further arithmetic here (exactly as ``gate_contact``
    already does).

    Also computes ``well_box_um`` -- a single well shape (per
    :data:`WELL_ENCLOSURE_MARGIN_UM`) enclosing every cell's (real and dummy)
    active region, the same "one shared tub for the whole matched group"
    shape :func:`_bjt_array_layout` draws for its own shared base well --
    used only when the caller requests ``flavor="pfet"``.

    ``draw_well_tap`` (issue #1473) additionally computes ``well_tap`` (see
    :func:`_mos_array_well_tap_layout`) -- the well-tie pad only a PDK
    family with a ``"well_tap_implant"`` role needs -- and grows
    ``well_box_um`` to enclose it too. ``None`` (the default, ``False``) when
    not requested, so every caller that never asks for it keeps byte-for-byte
    identical geometry.

    ``add_guard_ring`` (issue #1493) additionally encloses the array in an
    automatically-sized tap/guard ring (``_ring_layout``, the same helper
    ``diff_pair``/``bjt_array``/``esd_device`` already compose), sized off
    ``well_box_um`` -- the array's own shared-footprint box regardless of
    ``flavor`` (it is computed unconditionally above; only *drawing* it as a
    well is gated on ``flavor == 'pfet'``) -- plus ``ring_padding_um``. This
    mirrors :func:`_bjt_array_layout`'s own ring-around-``well_box_um``
    composition (there fixed at :data:`BJT_COLLECTOR_GAP_UM` rather than a
    caller-supplied padding) more closely than :func:`_diff_pair_layout`'s
    (whose own core box always starts at the origin): ``well_box_um`` can
    start at a negative offset once ``dummy`` columns extend left of column
    0, so ``ring_offset_um`` is derived from ``well_box_um``'s own corner
    rather than assumed to be ``-(ring_w + padding)``. ``ring_gap_side``/
    ``ring_gap_um``/``ring_gap_offset_um`` cut one routing opening through the
    ring (#434), exactly as they do for every other ring-composing generator.
    ``None``/``(0.0, 0.0)`` (the defaults, ``add_guard_ring=False``) when not
    requested, so every existing caller keeps byte-for-byte identical
    geometry.

    ``contact_gate_offset_um``/``bottom_endcap_um``/``sd_implant_margin_um``
    (issue #1577) are forwarded to :func:`_mos_unit_layout` unchanged -- see
    that function's own docstring.

    ``interior_channel_um`` (issue #1531, Phase 1) reserves a navigable
    routing channel *inside* the array's own grid: when greater than zero it
    is added to both ``col_pitch``/``row_pitch`` on top of the fixed
    :data:`MIN_SAME_LAYER_SPACING_UM` gap already between adjacent unit
    devices, and every resulting inter-row/inter-column gap band (one per
    adjacent row pair when ``rows > 1``, one per adjacent column pair when
    ``cols > 1``) is reported back as a rectangle in
    ``navigable_regions_um`` -- metal-free space wide enough for `klt
    gen-compose` (a future phase, not this one) to route a ``waypoints_um``
    backbone through to an interior unit device's pin, rather than treating
    the array's whole footprint as one opaque obstacle (see the issue's "The
    gap" section). ``0.0`` (the default) adds nothing to either pitch and
    reports an empty ``navigable_regions_um`` list, so every existing caller
    keeps byte-for-byte identical geometry -- the same regression-safety
    convention this generator's other additive params (``add_guard_ring``,
    ``gate_pad_clearance_um``) already established. Reserving the channel
    here (rather than teaching `gen-compose` to peek inside an otherwise-
    opaque block) keeps the generator responsible for its own routability
    the same way it is already responsible for its own DRC cleanliness;
    ``res_array``/``diff_pair`` are expected to grow the same parameter in a
    later phase, not this one."""
    unit = _mos_unit_layout(
        w_um,
        l_um,
        fingers,
        gate_contact,
        finger_topology,
        gate_pad_clearance_um,
        contact_gate_offset_um,
        bottom_endcap_um,
        sd_implant_margin_um,
    )
    col_pitch = unit["total_len_um"] + MIN_SAME_LAYER_SPACING_UM
    row_pitch = unit["bbox_height_um"] + MIN_SAME_LAYER_SPACING_UM
    # Issue #1531 (Phase 1): a positive `interior_channel_um` widens both
    # pitches by exactly that amount -- `0.0` (the default) is a strict no-op
    # on both, so every existing caller's `col_pitch`/`row_pitch` (and thus
    # every cell's `x0_um`/`y0_um` below) stay byte-for-byte unchanged.
    channel_um = max(interior_channel_um, 0.0)
    if channel_um > 0.0:
        col_pitch += channel_um
        row_pitch += channel_um

    order = (
        _centroid_order(rows, cols)
        if topology == "common_centroid"
        else [(r, c) for r in range(rows) for c in range(cols)]
    )
    cells = [
        {"idx": idx, "row": r, "col": c, "x0_um": c * col_pitch, "y0_um": r * row_pitch}
        for idx, (r, c) in enumerate(order)
    ]

    dummy_cells = []
    for r in range(rows):
        for dc in range(1, dummy + 1):
            dummy_cells.append(
                {"row": r, "col": -dc, "x0_um": -dc * col_pitch, "y0_um": r * row_pitch}
            )
            dummy_cells.append(
                {
                    "row": r,
                    "col": cols - 1 + dc,
                    "x0_um": (cols - 1 + dc) * col_pitch,
                    "y0_um": r * row_pitch,
                }
            )

    all_cells = cells + dummy_cells
    min_x0 = min(c["x0_um"] for c in all_cells)
    max_x1 = max(c["x0_um"] + unit["total_len_um"] for c in all_cells)
    min_y0 = min(c["y0_um"] for c in all_cells)
    max_y1 = max(c["y0_um"] + unit["bbox_height_um"] for c in all_cells)

    # Issue #1531 (Phase 1): one navigable rectangle per interior row/column
    # gap band opened up by `channel_um` above -- spanning the array's full
    # real+dummy footprint in the direction perpendicular to the gap, so a
    # future `gen-compose` phase can subtract these from a block's obstacle
    # bbox and route a `waypoints_um` backbone through to an interior unit
    # device's pin. Grid positions are plain `(r, c)` indices, unaffected by
    # `topology` (`_centroid_order` only permutes which index each position
    # gets, never the position itself -- see its own docstring), so this
    # loop is independent of `order`/`cells` above. Empty when `channel_um`
    # is `0.0` (the default), matching every existing caller's unchanged
    # geometry.
    navigable_regions: list[tuple[float, float, float, float]] = []
    if channel_um > 0.0:
        if rows > 1:
            for r in range(rows - 1):
                navigable_regions.append(
                    (
                        min_x0,
                        r * row_pitch + unit["bbox_height_um"],
                        max_x1,
                        (r + 1) * row_pitch,
                    )
                )
        if cols > 1:
            for c in range(cols - 1):
                navigable_regions.append(
                    (
                        c * col_pitch + unit["total_len_um"],
                        min_y0,
                        (c + 1) * col_pitch,
                        max_y1,
                    )
                )

    margin = WELL_ENCLOSURE_MARGIN_UM
    well_box = (min_x0 - margin, min_y0 - margin, max_x1 + margin, max_y1 + margin)
    # `voltage_flavor` marker box (issue #1054, margin widened by #1577):
    # independent of `well_box` -- see `_voltage_flavor_mark_margin_um`'s own
    # docstring for why gf180mcu needs a bigger margin here than the well
    # itself does.
    mark_margin = voltage_flavor_mark_margin_um
    voltage_flavor_mark_box = (
        min_x0 - mark_margin,
        min_y0 - mark_margin,
        max_x1 + mark_margin,
        max_y1 + mark_margin,
    )

    well_tap = None
    if draw_well_tap:
        well_tap = _mos_array_well_tap_layout(min_x0, min_y0, max_x1, max_y1)
        tap_box = well_tap["active"]
        well_box = (
            min(well_box[0], tap_box[0] - margin),
            min(well_box[1], tap_box[1] - margin),
            max(well_box[2], tap_box[2] + margin),
            max(well_box[3], tap_box[3] + margin),
        )

    ring = None
    ring_offset = (0.0, 0.0)
    if add_guard_ring:
        padding = ring_padding_um
        ring_w = GUARD_RING_DEFAULT_WIDTH_UM
        inner_x0 = well_box[0] - padding
        inner_y0 = well_box[1] - padding
        inner_w = (well_box[2] - well_box[0]) + 2 * padding
        inner_h = (well_box[3] - well_box[1]) + 2 * padding
        ring = _ring_layout(
            inner_w,
            inner_h,
            ring_w,
            GUARD_RING_DEFAULT_CONTACTS_PER_SIDE,
            ring_gap_side,
            ring_gap_um,
            ring_gap_offset_um,
        )
        ring_offset = _snap_offset_um(inner_x0 - ring_w, inner_y0 - ring_w)

    return {
        "unit": unit,
        "col_pitch_um": col_pitch,
        "row_pitch_um": row_pitch,
        "cells": cells,
        "dummy_cells": dummy_cells,
        "well_box_um": well_box,
        "voltage_flavor_mark_box_um": voltage_flavor_mark_box,
        "well_tap": well_tap,
        "ring": ring,
        "ring_offset_um": ring_offset,
        "navigable_regions_um": navigable_regions,
    }


def _res_unit_layout(
    length_um: float,
    width_um: float,
    via_min_w_um: float = 0.0,
    via_enclosure_min_um: float = 0.0,
) -> dict[str, Any]:
    """One unit resistor (or unit MoM/MiM cap cell footprint): a poly body
    of ``length_um`` between two contact+local-metal end pads.

    ``boxes_um["marker"]`` is the recognised resistive *body segment* -- the
    middle ``length_um``-wide span between the two ``contact_region_um`` end
    pads, deliberately excluding them. This is where the PDK's resistor-ID
    layer (and, on gf180mcu, its implant/salicide-block requires-layers) get
    drawn (issue #369): covering the whole unit (heads included) would
    consume the contacted end pads into the recognised body too, leaving no
    poly behind for the contacts to land on -- see
    ``klayout_tools.extract._resolve_resistors``, which subtracts the
    recognised body from the conductor region to derive the terminal poly.

    ``via_min_w_um``/``via_enclosure_min_um`` (issue #1639, ``0.0`` by
    default) are ``res_array``'s ``metal_level`` per-level geometry floors --
    see :data:`_PDK_METAL_RES_LEVEL_MIN_UM` -- applied as ``max(generic,
    floor)`` exactly like :func:`_cap_unit_layout`'s own ``*_min_*``
    arguments, so the default ``0.0`` leaves every existing (poly-body) caller
    byte-for-byte unchanged. Despite the ``poly``/``contact``/``metal`` box
    keys' names (kept as-is so ``_ResArrayPCell.produce_impl`` needs no
    change), this same geometry is what a ``metal_level`` request draws its
    ``metN`` body / end via / landing pad on -- see
    :func:`_resistor_layer_params`'s own docstring for how those roles are
    resolved to different physical layers depending on ``params.metal_level``.
    """
    contact_side_um = max(CONTACT_SIZE_UM, via_min_w_um)
    enclosure_um = max(ENCLOSURE_MARGIN_UM, via_enclosure_min_um)
    contact_region_um = contact_side_um + 2 * enclosure_um
    total_len_um = 2 * contact_region_um + length_um
    seg_positions = [
        (0.0, contact_region_um),
        (total_len_um - contact_region_um, total_len_um),
    ]

    boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "poly": [(0.0, 0.0, total_len_um, width_um)],
        "contact": [],
        "metal": [],
        "marker": [(contact_region_um, 0.0, contact_region_um + length_um, width_um)],
    }
    contact_half = contact_side_um / 2.0
    for sx0, sx1 in seg_positions:
        boxes["metal"].append((sx0, 0.0, sx1, width_um))
        cx = (sx0 + sx1) / 2.0
        cy = width_um / 2.0
        # Snap the centre to the dbu grid *before* deriving the edges -- an
        # un-snapped `cx +/- contact_half` lets the two edges round in
        # opposite directions when the centre lands on a half-dbu tie (e.g.
        # length_um=1.4965), silently drawing a 219nm contact against
        # gf180mcu's 220nm `contact.width.1` floor (issue #1551; same
        # failure mode `_snap_square_box_um` was introduced for in #685).
        boxes["contact"].append(_snap_square_box_um(cx, cy, contact_half, _GRID_DBU_UM))

    a_xy = ((seg_positions[0][0] + seg_positions[0][1]) / 2.0, width_um / 2.0)
    b_xy = ((seg_positions[-1][0] + seg_positions[-1][1]) / 2.0, width_um / 2.0)
    return {
        "total_len_um": total_len_um,
        "height_um": width_um,
        "boxes_um": boxes,
        "a_xy": a_xy,
        "b_xy": b_xy,
    }


def _res_array_layout(
    length_um: float,
    width_um: float,
    spacing_um: float,
    num: int,
    dummy: int,
    rows: int = 1,
    via_min_w_um: float = 0.0,
    via_enclosure_min_um: float = 0.0,
    min_spacing_um: float = 0.0,
) -> dict[str, Any]:
    """``num`` matched unit resistors (see :func:`_res_unit_layout`), folded
    into ``rows`` parallel rows in boustrophedon ("snake") order once
    ``rows > 1`` -- row 0 runs left to right, row 1 right to left, and so on
    -- so consecutive unit indices ``i``/``i + 1`` (the series-chain order
    implied by the ``R<i>_A``/``R<i>_B`` port-naming convention) stay
    physically adjacent across a row transition, the same way a real
    interdigitated resistor ladder folds a long string into a compact,
    roughly-square footprint instead of one long strip (issue #415).
    ``rows=1`` (the default) reproduces the original single-row layout
    exactly, byte-for-byte in cell placement.

    Row-to-row pitch mirrors :func:`_mos_array_layout`'s own row pitch (unit
    height plus the fixed :data:`MIN_SAME_LAYER_SPACING_UM` margin) rather
    than the caller-supplied ``spacing_um``, which stays scoped to
    within-row (horizontal) unit spacing only, per its documented meaning.

    Dummy elements (``dummy`` per end) extend the row that the real chain's
    first/last unit sits in, continuing in that row's own fold direction --
    for ``rows=1`` this is exactly the original "before index 0 / after
    index num - 1" placement.

    Each real cell carries a ``direction`` (+1/-1): a unit's own poly body
    is drawn identically regardless of row direction (never mirrored), so
    on a right-to-left row the physically-adjacent pad pair for a short
    row-transition jumper is the *reverse* of the left-to-right case --
    ``direction`` lets :func:`_res_array_describe`'s port-naming swap which
    physical pad (``a_xy``/``b_xy``) reports as ``_A``/``_B`` so ``R<i>_B``
    stays physically next to ``R<i + 1>_A`` in every row, not just even
    ones.

    ``via_min_w_um``/``via_enclosure_min_um`` (issue #1639, forwarded
    unchanged to :func:`_res_unit_layout`) and ``min_spacing_um`` are
    ``metal_level``'s own per-level geometry floors (see
    :data:`_PDK_METAL_RES_LEVEL_MIN_UM`) -- ``min_spacing_um`` is this level's
    own minimum spacing between adjacent unit resistors' end vias, applied to
    ``spacing_um`` as ``max(spacing_um, min_spacing_um)`` exactly like
    :func:`_cap_array_layout`'s own ``min_spacing_um`` floor, and reported
    back as this function's own ``spacing_um`` so the caller (
    :func:`_res_array_describe`) can note when it widened the request. All
    three default to ``0.0``, leaving every existing (poly-body) caller
    byte-for-byte unchanged.
    """
    unit = _res_unit_layout(length_um, width_um, via_min_w_um, via_enclosure_min_um)
    effective_spacing_um = max(spacing_um, min_spacing_um)
    pitch = unit["total_len_um"] + effective_spacing_um
    # Row-to-row pitch (issue #1639): the same `min_spacing_um` floor applies
    # here too -- it is the *same* same-layer same-metal spacing rule
    # (sky130's `met5.space.1`) that binds between two body shapes whether
    # they are adjacent within one row or across a row transition, so using
    # the generic `MIN_SAME_LAYER_SPACING_UM` unconditionally here (while
    # `pitch` above already floors under `min_spacing_um`) would leave a
    # folded (`rows > 1`) met5 request DRC-dirty even though the unfolded
    # case is clean.
    row_pitch = unit["height_um"] + max(MIN_SAME_LAYER_SPACING_UM, min_spacing_um)
    cols_per_row = -(-num // rows) if rows > 0 else num  # ceil(num / rows)

    def _row_and_column(i: int) -> tuple[int, int, int]:
        """(row, column, direction) for unit index ``i`` under the
        boustrophedon fold -- ``direction`` is +1 for a left-to-right row,
        -1 for a right-to-left one."""
        row = i // cols_per_row
        local_j = i % cols_per_row
        direction = 1 if row % 2 == 0 else -1
        column = local_j if direction == 1 else cols_per_row - 1 - local_j
        return row, column, direction

    cells = []
    for i in range(num):
        row, column, direction = _row_and_column(i)
        cells.append(
            {
                "idx": i,
                "x0_um": column * pitch,
                "y0_um": row * row_pitch,
                "direction": direction,
            }
        )

    dummy_cells = []
    first_row, first_column, first_direction = _row_and_column(0)
    last_row, last_column, last_direction = _row_and_column(num - 1)
    for dc in range(1, dummy + 1):
        dummy_cells.append(
            {
                "x0_um": (first_column - first_direction * dc) * pitch,
                "y0_um": first_row * row_pitch,
            }
        )
        dummy_cells.append(
            {
                "x0_um": (last_column + last_direction * dc) * pitch,
                "y0_um": last_row * row_pitch,
            }
        )
    return {
        "unit": unit,
        "pitch_um": pitch,
        "row_pitch_um": row_pitch,
        "cols_per_row": cols_per_row,
        "cells": cells,
        "dummy_cells": dummy_cells,
        "spacing_um": effective_spacing_um,
    }


def _cap_unit_layout(
    plate_w_um: float,
    plate_h_um: float,
    top_via_metal_min_w_um: float = 0.0,
    top_via_min_w_um: float = 0.0,
    bottom_plate_margin_min_um: float = 0.0,
) -> dict[str, Any]:
    """One unit MiM capacitor cell: a ``plate_w_um`` x ``plate_h_um``
    top-plate mark (sky130's ``capm``) centred over a larger bottom-plate
    conductor (``met3``), with a top-plate via + local-metal landing pad
    (``via3``/``met4``) centred on the top plate -- the capacitor sibling of
    :func:`_res_unit_layout`, with a single centred via/pad standing in for
    that function's two end contacts (a MiM cap has no resistive body to
    keep a via clear of, so the via lands in the middle of the plate rather
    than at either end).

    The three ``*_min_*`` arguments are per-PDK-family geometry *floors*
    (issues #1455/#1555, resolved by :func:`_cap_geometry_min_um` from
    :data:`_PDK_CAP_GEOMETRY_MIN_UM`), each applied as ``max(generic,
    floor)`` so the default ``0.0`` leaves a family that overrides nothing --
    sky130 -- byte-for-byte unchanged:

    - ``top_via_metal_min_w_um`` widens the drawn landing pad (and the escape
      stub/pad drawn on the same layer) past the generic
      ``CONTACT_SIZE_UM + 2*ENCLOSURE_MARGIN_UM`` when a family's own
      top-plate-via-metal layer carries a coarser minimum-width rule
      (sg13g2's ``TopMetal1``, 1.64um vs. sky130's ``met4``, 0.3um).
    - ``top_via_min_w_um`` widens the drawn via past ``CONTACT_SIZE_UM`` when
      the family's top-plate via has a coarser minimum size than the
      *contact* rule that constant was sized against (gf180mcu's ``Via4``,
      0.26um).
    - ``bottom_plate_margin_min_um`` widens the bottom plate past
      ``CAP_BOTTOM_PLATE_MARGIN_UM`` for a family whose bottom plate is the
      DRM's oversized "virtual bottom plate" rather than an ordinary
      conductor (gf180mcu's ``Metal4``, drawn at
      ``CapacitorDevice.bottom_plate_oversize_um`` = 1.06um so the drawn
      shape *is* the derived virtual plate).

    On top of the via-enclosing landing pad centred on the plate (which sits
    directly over the bottom plate -- unroutable in isolation, issue #1494),
    this also draws a same-layer escape: a stub running north from that
    centred pad, clear past the bottom plate's own top edge by
    :data:`CAP_TOP_ESCAPE_CLEARANCE_UM`, ending in a second landing pad
    (``top_escape_xy``) whose own bounding box no longer touches -- let alone
    overlaps -- the bottom plate. A via stack landed at ``top_escape_xy``
    using the family's own metal/via stack can step straight down without
    ever crossing the bottom plate's footprint, unlike the centred
    ``top_xy`` (kept for reference/backward compatibility) that a further
    via step would silently short to the bottom plate."""
    margin = max(CAP_BOTTOM_PLATE_MARGIN_UM, bottom_plate_margin_min_um)
    bottom_w = plate_w_um + 2 * margin
    bottom_h = plate_h_um + 2 * margin
    cx, cy = bottom_w / 2.0, bottom_h / 2.0

    via_side = max(CONTACT_SIZE_UM, top_via_min_w_um)
    via_half = via_side / 2.0
    # The landing pad keeps its `ENCLOSURE_MARGIN_UM` clearance around
    # whatever via was actually drawn -- so a family whose via floor widened
    # the cut (gf180mcu's `Via4`) widens the pad with it, rather than
    # silently eating into the enclosure. Identical to the pre-#1555
    # expression on every family whose via is the generic `CONTACT_SIZE_UM`.
    pad_side = max(via_side + 2 * ENCLOSURE_MARGIN_UM, top_via_metal_min_w_um)
    pad_half = pad_side / 2.0
    top_via_box = _snap_square_box_um(cx, cy, via_half, _GRID_DBU_UM)
    top_via_metal_box = _snap_square_box_um(cx, cy, pad_half, _GRID_DBU_UM)

    # Escape stub + landing pad (issue #1494): same layer/width as
    # `top_via_metal_box` (so it inherits that pad's own family-specific
    # minimum-width compliance, e.g. sg13g2's `TopMetal1`), running straight
    # north from the centred pad's top edge to a landing pad entirely beyond
    # `bottom_h` -- i.e. entirely outside the bottom plate's own bounding
    # box, not merely touching it.
    escape_pad_cy = bottom_h + CAP_TOP_ESCAPE_CLEARANCE_UM + pad_half
    escape_pad_box = _snap_square_box_um(cx, escape_pad_cy, pad_half, _GRID_DBU_UM)
    escape_stub_box = (
        cx - pad_half,
        top_via_metal_box[3],
        cx + pad_half,
        escape_pad_box[1],
    )

    boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "bottom_plate": [(0.0, 0.0, bottom_w, bottom_h)],
        "top_plate": [(margin, margin, margin + plate_w_um, margin + plate_h_um)],
        "top_via": [top_via_box],
        "top_via_metal": [top_via_metal_box, escape_stub_box, escape_pad_box],
    }
    return {
        "total_w_um": bottom_w,
        "total_h_um": bottom_h,
        "boxes_um": boxes,
        # The landing-pad side actually used above -- read back by
        # `_cap_array_describe` for its `C<i>_TOP` port width instead of that
        # function recomputing the sizing rule, so the reported width can
        # never drift from the drawn pad as more per-family floors land
        # (issue #1555; the value is unchanged for sky130/sg13g2).
        "top_pad_w_um": pad_side,
        # Bottom-plate port: the conductor's own local-left edge, mirroring
        # `_res_unit_layout`'s `a_xy`. Legacy top-plate centre: the
        # via/landing-pad centre -- the same point `top_via`/the original
        # `top_via_metal` pad are drawn at (issue #1494: unroutable, kept
        # only for internal reference). Escape top-plate port: the landing
        # pad past the bottom plate's own bbox -- the point
        # `_cap_array_describe` reports as `C{idx}_TOP` today.
        "bot_xy": (0.0, cy),
        "top_xy": (cx, cy),
        "top_escape_xy": (cx, escape_pad_cy),
    }


def _cap_array_layout(
    plate_w_um: float,
    plate_h_um: float,
    spacing_um: float,
    num: int,
    top_via_metal_min_w_um: float = 0.0,
    top_via_min_w_um: float = 0.0,
    bottom_plate_margin_min_um: float = 0.0,
    min_spacing_um: float = 0.0,
) -> dict[str, Any]:
    """``num`` matched unit MiM capacitors (see :func:`_cap_unit_layout`) in
    a single row, spaced ``spacing_um`` apart -- the capacitor sibling of
    :func:`_res_array_layout`'s own (unfolded, single-row) layout. No
    ``rows`` folding or ``dummy`` padding yet (issue #1117 scopes this
    generator's first increment to the core plate/via geometry; both are
    natural `res_array`-parity follow-ups, not correctness gaps -- a MiM
    cap array with only a handful of matched units rarely needs either).

    ``top_via_metal_min_w_um``/``top_via_min_w_um``/
    ``bottom_plate_margin_min_um`` are forwarded to :func:`_cap_unit_layout`
    unchanged (issues #1455/#1555).

    ``min_spacing_um`` (issue #1555) is this PDK family's own floor under the
    requested ``spacing_um``: gf180mcu's ``mim.space.1`` (MIMTM.1) requires
    1.2um between the virtual bottom plates of adjacent MiM capacitors, far
    more than this generator's 0.5um default or its generic
    ``MIN_SAME_LAYER_SPACING_UM`` advisory, so a request that would otherwise
    draw a guaranteed violation is widened instead of rejected (the effective
    spacing is what :func:`_cap_array_describe` reports as
    ``drc_hints.min_spacing_um``, alongside a note). ``0.0`` -- every family
    but gf180mcu -- leaves the requested spacing exactly as asked."""
    unit = _cap_unit_layout(
        plate_w_um,
        plate_h_um,
        top_via_metal_min_w_um,
        top_via_min_w_um,
        bottom_plate_margin_min_um,
    )
    effective_spacing_um = max(spacing_um, min_spacing_um)
    pitch = unit["total_w_um"] + effective_spacing_um
    cells = [{"idx": i, "x0_um": i * pitch, "y0_um": 0.0} for i in range(num)]
    return {
        "unit": unit,
        "pitch_um": pitch,
        "cells": cells,
        "spacing_um": effective_spacing_um,
    }


#: The four sides a ring gap (``ring_gap_side``) can be cut on, and the
#: outward normal (``direction_deg``) each one's reported port faces.
RING_SIDE_DIRECTIONS: dict[str, int] = {"N": 90, "S": 270, "E": 0, "W": 180}


def _ring_gap_layout(
    outer_w_um: float,
    outer_h_um: float,
    ring_w_um: float,
    side: str,
    gap_um: float,
    gap_offset_um: float,
) -> dict[str, Any]:
    """One opening ("gap") cut through a ring's band on ``side`` (#434).

    The opening spans the ring's full thickness on that side and ``gap_um``
    along it, centred on the side's midpoint plus ``gap_offset_um`` (positive
    is toward +x on ``"N"``/``"S"``, toward +y on ``"E"``/``"W"``). Exactly
    *one* side may be cut (enforced by the per-generator validators), so the
    ring stays a single connected C-shaped conductor -- two openings would
    split it into two electrically separate arcs while its tap ports still
    claimed one net.

    Returns the cut ``box_um`` (subtracted from the ring by
    :func:`_insert_ring`), the opening's centre (``x_um``/``y_um``, on the
    ring's own centre line for that side, i.e. where a ``GAP_<side>`` port is
    reported), its ``opening_um`` length, and the ``span_um`` interval it
    covers along the side's axis.
    """
    half = gap_um / 2.0
    if side in ("N", "S"):
        cx = outer_w_um / 2.0 + gap_offset_um
        cy = outer_h_um - ring_w_um / 2.0 if side == "N" else ring_w_um / 2.0
        y0 = outer_h_um - ring_w_um if side == "N" else 0.0
        y1 = outer_h_um if side == "N" else ring_w_um
        box = (cx - half, y0, cx + half, y1)
        span = (cx - half, cx + half)
    else:
        cy = outer_h_um / 2.0 + gap_offset_um
        cx = outer_w_um - ring_w_um / 2.0 if side == "E" else ring_w_um / 2.0
        x0 = outer_w_um - ring_w_um if side == "E" else 0.0
        x1 = outer_w_um if side == "E" else ring_w_um
        box = (x0, cy - half, x1, cy + half)
        span = (cy - half, cy + half)

    return {
        "side": side,
        "box_um": box,
        "x_um": cx,
        "y_um": cy,
        "opening_um": gap_um,
        "span_um": span,
    }


def _auto_ring_inner_size_um(info: dict[str, Any]) -> tuple[float, float]:
    """Inner (protected-area) size of the automatically-sized ring in a
    ``diff_pair``/``bjt_array`` layout dict -- the straight run available on
    each ring side, which is what a ring opening has to fit inside
    (:func:`~klayout_tools.gen_describe._validate_ring_gap`). ``(0.0, 0.0)``
    when the layout draws no ring."""
    ring = info.get("ring")
    if ring is None:
        return (0.0, 0.0)
    x0, y0, x1, y1 = ring["inner_box_um"]
    return (x1 - x0, y1 - y0)


def _boxes_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    """Whether two axis-aligned boxes overlap with non-zero area (a shared
    edge alone is not an overlap)."""
    eps = 1e-9
    return (
        min(a[2], b[2]) - max(a[0], b[0]) > eps
        and min(a[3], b[3]) - max(a[1], b[1]) > eps
    )


def _snap_square_box_um(
    cx_um: float, cy_um: float, half_um: float, dbu_um: float
) -> tuple[float, float, float, float]:
    """A ``2 * half_um``-wide square centred on ``(cx_um, cy_um)``, snapped
    to the ``dbu_um`` manufacturing grid *before* the box edges are derived,
    so the returned box's width and height are exactly ``2 * round(half_um /
    dbu_um) * dbu_um`` regardless of where the un-snapped centre falls
    relative to the grid.

    This matters because :func:`_insert_boxes`/``_insert_ring``'s ``_to_box``
    round each of a box's *four edges* to the dbu grid independently
    (``int(round(x0 / dbu))`` ... ``int(round(x1 / dbu))``). Building the box
    from an un-snapped float centre first (``cx - half``, ``cx + half``) lets
    ordinary floating-point noise put one edge just below a half-dbu grid
    boundary and the other just above it -- Python's banker's-rounding
    ``round()`` can then round those two edges in *different* directions,
    silently drawing a box 1 dbu narrower/shorter than intended. That is
    exactly the ``contact.width.1`` failure mode diagnosed in issue #685:
    ``CONTACT_SIZE_UM`` carries zero DRC margin above gf180mcu's ``CO.1``
    threshold, so a single dbu of asymmetric rounding is enough to trip it.
    Snapping the centre (and the half-width, which is already grid-exact for
    every current caller) to integer dbu counts *first* makes both edges
    derive from the same rounded integer, so they can never drift apart.
    """
    cx_dbu = round(cx_um / dbu_um)
    cy_dbu = round(cy_um / dbu_um)
    half_dbu = round(half_um / dbu_um)
    return (
        (cx_dbu - half_dbu) * dbu_um,
        (cy_dbu - half_dbu) * dbu_um,
        (cx_dbu + half_dbu) * dbu_um,
        (cy_dbu + half_dbu) * dbu_um,
    )


def _snap_offset_um(
    ox_um: float, oy_um: float, dbu_um: float = _GRID_DBU_UM
) -> tuple[float, float]:
    """A placement offset ``(ox_um, oy_um)`` snapped to the ``dbu_um``
    manufacturing grid, so boxes already grid-exact in their own local frame
    stay grid-exact after the offset is applied.

    This is the placement-offset counterpart of :func:`_snap_square_box_um`
    (issue #2442): :func:`_insert_boxes`/``_insert_ring`` round each of a
    box's four edges *independently* after adding the offset
    (``int(round((x0 + ox_um) / dbu))`` ...). An un-snapped offset derived
    from an arbitrary float param (e.g. ``ring_padding_um``, whose only
    documented constraint is ``>= 0``) can land one edge of an exactly
    ``CONTACT_SIZE_UM``-square contact just below a half-dbu rounding
    boundary and the opposite edge just above it, silently drawing a
    221x220dbu contact that trips gf180mcu's ``contact.width.1`` max-size
    bound. Snapping the offset to integer dbu *first* makes every shifted
    edge derive from the same grid as the un-shifted box, so per-edge
    rounding can never drift an edge pair apart."""
    return (round(ox_um / dbu_um) * dbu_um, round(oy_um / dbu_um) * dbu_um)


def _ring_layout(
    inner_w_um: float,
    inner_h_um: float,
    ring_w_um: float,
    contacts_per_side: int | tuple[int, int],
    gap_side: str = "",
    gap_um: float = 0.0,
    gap_offset_um: float = 0.0,
) -> dict[str, Any]:
    """A tap/metal ring (drawn as an outer-box-minus-inner-box boolean, see
    :func:`_insert_ring`, so it is always one unbroken polygon -- no
    same-layer spacing violation between ring segments) around an
    ``inner_w_um`` x ``inner_h_um`` protected area, with contacts evenly
    spaced along each of the four sides.

    ``contacts_per_side`` is either a single ``int`` -- the historical
    behaviour, applied uniformly to all four sides -- or an ``(ns, ew)``
    tuple: ``ns`` contacts on each of the N/S (top/bottom) sides, spaced
    along ``inner_w_um``, and ``ew`` contacts on each of the E/W (left/
    right) sides, spaced along ``inner_h_um``. A caller sizing a ring around
    a strongly non-square inner region can then target an independent pitch
    on each axis instead of having the achievable count capped by whichever
    side is shorter (issue #685).

    ``gap_side`` (``""`` = closed ring, the default) cuts one routing opening
    through the ring on that side (#434) -- see :func:`_ring_gap_layout`. A
    contact that would be clipped by the opening is dropped, and the side's
    own tap port is dropped from ``ports`` when its midpoint falls inside the
    opening (there is no metal left under it there); the ring stays one
    connected conductor either way, so the remaining tap ports still describe
    a single tap net.
    """
    outer_w = inner_w_um + 2 * ring_w_um
    outer_h = inner_h_um + 2 * ring_w_um
    contact_half = CONTACT_SIZE_UM / 2.0
    contacts_ns, contacts_ew = (
        contacts_per_side
        if isinstance(contacts_per_side, tuple)
        else (contacts_per_side, contacts_per_side)
    )

    gap = (
        _ring_gap_layout(outer_w, outer_h, ring_w_um, gap_side, gap_um, gap_offset_um)
        if gap_side
        else None
    )

    centers: list[tuple[float, float]] = []
    for i in range(contacts_ns):
        t = (i + 1) / (contacts_ns + 1)
        cx = ring_w_um + t * inner_w_um
        centers.append((cx, outer_h - ring_w_um / 2.0))  # top
        centers.append((cx, ring_w_um / 2.0))  # bottom
    for i in range(contacts_ew):
        t = (i + 1) / (contacts_ew + 1)
        cy = ring_w_um + t * inner_h_um
        centers.append((ring_w_um / 2.0, cy))  # left
        centers.append((outer_w - ring_w_um / 2.0, cy))  # right

    contact_boxes = [
        _snap_square_box_um(cx, cy, contact_half, _GRID_DBU_UM) for (cx, cy) in centers
    ]
    if gap is not None:
        contact_boxes = [
            box for box in contact_boxes if not _boxes_overlap(box, gap["box_um"])
        ]

    ports = {
        "N": (outer_w / 2.0, outer_h - ring_w_um / 2.0),
        "S": (outer_w / 2.0, ring_w_um / 2.0),
        "E": (outer_w - ring_w_um / 2.0, outer_h / 2.0),
        "W": (ring_w_um / 2.0, outer_h / 2.0),
    }
    if gap is not None:
        px, py = ports[gap["side"]]
        along = px if gap["side"] in ("N", "S") else py
        lo, hi = gap["span_um"]
        if lo - 1e-9 <= along <= hi + 1e-9:
            del ports[gap["side"]]

    return {
        "outer_w_um": outer_w,
        "outer_h_um": outer_h,
        "outer_box_um": (0.0, 0.0, outer_w, outer_h),
        "inner_box_um": (
            ring_w_um,
            ring_w_um,
            ring_w_um + inner_w_um,
            ring_w_um + inner_h_um,
        ),
        "contact_boxes_um": contact_boxes,
        "gap": gap,
        "ports": ports,
    }


#: Order ``well_island`` prefers when picking which ring side carries its net
#: label text (see :func:`_well_island_label_point`). Any side still covered
#: by ring metal works electrically -- the order only makes the choice
#: deterministic, so the drawn GDS and the reported label position never
#: disagree between two runs (or between ``produce_impl`` and ``describe``).
_WELL_ISLAND_LABEL_SIDE_ORDER = ("S", "N", "W", "E")


def _well_island_label_point(ring: dict[str, Any]) -> tuple[float, float] | None:
    """Where ``well_island`` places its net-label text: the midpoint of the
    first side of ``ring`` (in :data:`_WELL_ISLAND_LABEL_SIDE_ORDER`) that is
    still covered by ring metal.

    A point on a side's own centre line always falls *inside* the drawn metal
    band, which is what makes the text attach to that conductor during
    extraction. ``None`` only when the ring has no covered side left at all
    -- structurally impossible today (``ring_gap_side`` opens at most one
    side), but handled rather than assumed.
    """
    for side in _WELL_ISLAND_LABEL_SIDE_ORDER:
        xy = ring["ports"].get(side)
        if xy is not None:
            return xy
    return None


def _well_box_um(
    ring: dict[str, Any], margin_um: float
) -> tuple[float, float, float, float]:
    """The well rectangle enclosing ``ring``'s outer box by ``margin_um`` on
    every side -- the same shape ``guard_ring``'s ``add_well`` already draws,
    factored out so ``well_island``'s isolation math and its ``produce_impl``
    can never derive it differently."""
    return (
        -margin_um,
        -margin_um,
        ring["outer_w_um"] + margin_um,
        ring["outer_h_um"] + margin_um,
    )


def _box_separation_um(
    a_um: tuple[float, float, float, float],
    b_um: tuple[float, float, float, float],
) -> float:
    """Euclidian separation (um) between two axis-aligned rectangles: ``0.0``
    when they touch or overlap, otherwise the corner-to-corner distance
    between their nearest points.

    Euclidian rather than per-axis on purpose -- it is the metric the well
    isolation rules this module enforces are themselves written with (sky130's
    ``nwell.isolated(1.27, euclidian)``; see :data:`_PDK_WELL_ISOLATION_UM`),
    and it is the *stricter* of the two at a diagonal offset, so a
    geometry this function accepts is never one the rule's own metric would
    reject.
    """
    ax0, ay0, ax1, ay1 = a_um
    bx0, by0, bx1, by1 = b_um
    dx = max(0.0, bx0 - ax1, ax0 - bx1)
    dy = max(0.0, by0 - ay1, ay0 - by1)
    return math.hypot(dx, dy)


def _parse_well_regions(
    generator: str, param_name: str, raw: Any
) -> list[dict[str, Any]]:
    """Parse a ``params.isolate_from``-shaped value into normalised well
    regions (issue #1421).

    Each entry is ``[x0, y0, x1, y1]`` or ``[x0, y0, x1, y1, "<net>"]`` -- a
    rectangle in the generated cell's **own** coordinate frame (the same frame
    the response's ``bbox_um``/``ports[]`` use), optionally tagged with the
    net that region is held at. The optional net is what makes the
    "equipotential wells may merge" case expressible: a neighbour tagged with
    the *same* net as the island's own tie is not a different-potential
    neighbour, so the isolation rule does not apply to it (see
    :func:`~klayout_tools.gen_describe._well_island_isolation`).

    Corner order is normalised (``min``/``max``), so a caller who hands over a
    bbox with either corner first gets the same answer. A degenerate
    (zero-area) rectangle is rejected -- it is far more likely a caller bug
    than an intentional request, and it would silently satisfy every
    separation check by having no extent to violate one with.
    """
    if not isinstance(raw, list):
        raise GenError(
            f"generator '{generator}': params.{param_name} must be a JSON array"
        )
    regions: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        where = f"params.{param_name}[{index}]"
        if not isinstance(entry, (list, tuple)) or len(entry) not in (4, 5):
            raise GenError(
                f"generator '{generator}': {where} must be "
                "[x0_um, y0_um, x1_um, y1_um] or "
                "[x0_um, y0_um, x1_um, y1_um, net]"
            )
        coords: list[float] = []
        for value in entry[:4]:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise GenError(
                    f"generator '{generator}': {where}'s first four elements "
                    "must be numbers (a rectangle in um)"
                )
            coords.append(float(value))
        x0, y0, x1, y1 = coords
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if box[0] == box[2] or box[1] == box[3]:
            raise GenError(
                f"generator '{generator}': {where} is a zero-area rectangle "
                f"({coords}) -- a well region with no extent cannot be "
                "separated from anything"
            )
        net: str | None = None
        if len(entry) == 5:
            if not isinstance(entry[4], str):
                raise GenError(
                    f"generator '{generator}': {where}'s optional fifth "
                    "element must be a net name string"
                )
            net = entry[4] or None
        regions.append({"box_um": box, "net": net})
    return regions


def _diff_pair_layout(
    w_um: float,
    l_um: float,
    splits: int,
    add_guard_ring: bool,
    ring_gap_side: str = "",
    ring_gap_um: float = 0.0,
    ring_gap_offset_um: float = 0.0,
    ring_padding_um: float = GUARD_RING_DEFAULT_PADDING_UM,
    row_spacing_um: float = MIN_SAME_LAYER_SPACING_UM,
    gate_contact: bool = False,
    gate_pad_clearance_um: float = 0.0,
    contact_gate_offset_um: float = 0.0,
    bottom_endcap_um: float = 0.0,
    sd_implant_margin_um: float = 0.0,
) -> dict[str, Any]:
    """Two matched devices (``"A"``/``"B"``), each split into ``splits``
    unit sub-instances, interleaved in a true common-centroid cross-quad
    checkerboard over a 2-row x ``splits``-col grid: ``label(row, col) = "A"
    if (row + col) is even else "B"`` -- for ``splits=2`` this is exactly the
    classic differential-pair "A B / B A" layout; it generalises the same
    way for any ``splits``, each column always holding one A and one B.

    Both rows place their unit device at the same ``x0_um = col *
    col_pitch``, so the two legs **share one x column per terminal** -- A's
    and B's ports in a given column differ only in ``y_um`` (issue #1495).
    That is a property of the interleave, not an oversight: separating the
    legs in x by a constant per-leg offset would shift one leg's x centroid
    relative to the other's, destroying exactly the gradient cancellation
    this layout exists to provide. (Nor is there a cleverer uniform-pitch
    ordering for odd ``splits``: placing ``2 * splits`` units at distinct
    multiples of one pitch and partitioning them into two equal-x-centroid
    halves needs the position sum ``splits * (2 * splits - 1)`` to be even,
    which fails for every odd ``splits``.) The caveat is documented for
    callers in ``docs/cli/gen.md`` and surfaced in ``klt gen --list``.

    ``ring_padding_um``/``row_spacing_um`` (issue #484) are the ring-to-core
    padding and the device-row-to-device-row gap, both fixed at their
    present-day values by default so omitting them reproduces prior geometry
    byte-for-byte; a caller that needs more room to route both matched
    devices' gate nets out of the block can grow either and pay the area.
    ``col_pitch`` (the within-row gap between interleaved splits) stays
    fixed to ``MIN_SAME_LAYER_SPACING_UM`` -- out of scope for #484.

    ``gate_contact`` (issue #492) is forwarded to :func:`_mos_unit_layout`;
    it grows each unit's ``bbox_height_um``, so both the row pitch and the
    automatically-sized guard ring follow it without any further arithmetic
    here. ``gate_pad_clearance_um`` (issue #1450) is forwarded the same way,
    with the same knock-on effect. ``contact_gate_offset_um``/
    ``bottom_endcap_um``/``sd_implant_margin_um`` (issue #1577) are likewise
    forwarded unchanged -- see :func:`_mos_unit_layout`'s own docstring.
    """
    unit = _mos_unit_layout(
        w_um,
        l_um,
        1,
        gate_contact,
        gate_pad_clearance_um=gate_pad_clearance_um,
        contact_gate_offset_um=contact_gate_offset_um,
        bottom_endcap_um=bottom_endcap_um,
        sd_implant_margin_um=sd_implant_margin_um,
    )
    col_pitch = unit["total_len_um"] + MIN_SAME_LAYER_SPACING_UM
    row_pitch = unit["bbox_height_um"] + row_spacing_um

    counts = {"A": 0, "B": 0}
    cells = []
    for row in range(2):
        for col in range(splits):
            label = "A" if (row + col) % 2 == 0 else "B"
            counts[label] += 1
            cells.append(
                {
                    "row": row,
                    "col": col,
                    "label": label,
                    "n": counts[label],
                    "x0_um": col * col_pitch,
                    "y0_um": row * row_pitch,
                }
            )

    core_w = splits * col_pitch - MIN_SAME_LAYER_SPACING_UM
    core_h = 2 * row_pitch - row_spacing_um

    ring = None
    ring_offset = (0.0, 0.0)
    if add_guard_ring:
        padding = ring_padding_um
        ring_w = GUARD_RING_DEFAULT_WIDTH_UM
        ring = _ring_layout(
            core_w + 2 * padding,
            core_h + 2 * padding,
            ring_w,
            GUARD_RING_DEFAULT_CONTACTS_PER_SIDE,
            ring_gap_side,
            ring_gap_um,
            ring_gap_offset_um,
        )
        ring_offset = _snap_offset_um(-(ring_w + padding), -(ring_w + padding))

    return {
        "unit": unit,
        "cells": cells,
        "col_pitch_um": col_pitch,
        "row_pitch_um": row_pitch,
        "core_w_um": core_w,
        "core_h_um": core_h,
        "ring": ring,
        "ring_offset_um": ring_offset,
    }


#: Gap (um) kept between the shared base well and the collector guard ring
#: that surrounds it -- exceeds gf180mcu's ``comp.space.1`` (0.28um), so the
#: collector COMP ring never crowds the emitter/base COMP inside the well.
BJT_COLLECTOR_GAP_UM = 0.4

#: Margin (um) the per-unit bipolar device-mark box (``boxes_um["marker"]``,
#: see :func:`_bjt_unit_layout`) is grown beyond the emitter pad it encloses,
#: on every side. Two independent reasons this must be strictly positive
#: (issue #432):
#:
#: 1. A device-mark box exactly coincident with the emitter pad makes
#:    ``base == emitter`` in `extract.py` (``base = active & marker``,
#:    ``emitter = active & base``) -- `klt extract`'s
#:    ``DeviceExtractorBJT3Transistor`` then finds no base-minus-emitter
#:    extension to derive a collector terminal from and aborts with an
#:    unhandled ``RuntimeError`` (the "coincident-marker" bug this issue
#:    fixes) instead of the documented clean error envelope.
#: 2. Reuses `MIN_SAME_LAYER_SPACING_UM`'s existing 0.4um unit-to-unit gap:
#:    growing the marker by `ENCLOSURE_MARGIN_UM` (0.1um) on every side still
#:    leaves 0.3um of clearance to the base-tie pad (same unit) and to every
#:    neighbouring unit/row -- comfortably above gf180mcu's
#:    ``bjt.separation.comp.1`` 0.1um threshold (see `decks/gf180mcu.py`), so
#:    the per-unit marker never trips that DRC rule.
BJT_MARK_GROWTH_UM = ENCLOSURE_MARGIN_UM


def _bjt_unit_layout(emitter_um: float) -> dict[str, Any]:
    """One vertical-PNP unit device, drawn from base layers (not a vendor
    library cell -- see ``docs/design/gen-bjt-array-spike.md``): a square
    emitter diffusion pad beside a base-tie diffusion pad, each with a
    contact and a covering local-metal pad, both sitting inside the array's
    shared base well.

    The two pads model the device's emitter (P+ in the Nwell base) and its
    base tie (N+ Nwell contact) as adjacent COMP/diff regions -- a
    DRC-clean, matching-faithful *floorplan* of the device, deliberately not
    a process-exact vertical cross-section (the curated decks check no
    implant layer, so emitter/base/collector all draw on the one ``active``
    role; that fidelity limit is recorded in the design note). The array's
    collector guard ring is drawn once at array level, not per unit (see
    :func:`_bjt_array_layout`).

    ``boxes_um["marker"]`` is the per-unit bipolar device-mark box (issue
    #432) -- the emitter pad grown by :data:`BJT_MARK_GROWTH_UM` on every
    side, deliberately *not* the whole unit (base-tie pad included): sky130's
    ``EXTRACTION_DECK.bipolars`` entry derives ``base = active & marker`` and
    ``emitter = active & base``, so a marker coincident with the whole unit
    (or the whole array, as this generator drew previously) would make every
    diffusion pad inside it -- the base-tie pad included -- look like a
    second emitter of the same device.

    ``boxes_um["tap"]`` is a ``tap``-role shape over the base-tie pad
    (coincident with ``base_box``) -- sky130's extraction deck reaches the
    shared base well's node through ``nwell -> tap -> contact -> metals``
    (see `extract.py`'s connectivity section); without a drawn ``tap`` shape
    there, the base-tie pad's own contact/metal stack has no path to the
    device's recognised base region and the base terminal resolves to a
    floating anonymous node instead of a real net.
    """
    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    gap = MIN_SAME_LAYER_SPACING_UM
    base_x0 = emitter_um + gap
    base_x1 = base_x0 + contact_region_um
    total_len_um = base_x1
    height_um = emitter_um

    emitter_box = (0.0, 0.0, emitter_um, height_um)
    base_box = (base_x0, 0.0, base_x1, height_um)
    marker_box = (
        -BJT_MARK_GROWTH_UM,
        -BJT_MARK_GROWTH_UM,
        emitter_um + BJT_MARK_GROWTH_UM,
        height_um + BJT_MARK_GROWTH_UM,
    )

    contact_half = CONTACT_SIZE_UM / 2.0
    ecx, ecy = emitter_um / 2.0, height_um / 2.0
    bcx, bcy = (base_x0 + base_x1) / 2.0, height_um / 2.0

    def _contact_box(cx: float, cy: float) -> tuple[float, float, float, float]:
        return (
            cx - contact_half,
            cy - contact_half,
            cx + contact_half,
            cy + contact_half,
        )

    contacts = [_contact_box(ecx, ecy), _contact_box(bcx, bcy)]

    boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "active": [emitter_box, base_box],
        "contact": contacts,
        "metal": [emitter_box, base_box],
        "marker": [marker_box],
        "tap": [base_box],
    }
    return {
        "total_len_um": total_len_um,
        "height_um": height_um,
        "boxes_um": boxes,
        "e_xy": (ecx, ecy),
        "b_xy": (bcx, bcy),
    }


def _bjt_array_layout(
    emitter_um: float,
    rows: int,
    cols: int,
    dummy: int,
    topology: str,
    add_collector_ring: bool,
    ring_gap_side: str = "",
    ring_gap_um: float = 0.0,
    ring_gap_offset_um: float = 0.0,
) -> dict[str, Any]:
    """A ``rows`` x ``cols`` common-centroid (or plain-array) grid of
    :func:`_bjt_unit_layout` unit devices, with ``dummy`` flanking columns
    each side, a single shared base well enclosing every unit's diffusion,
    and an optional collector guard ring (drawn via :func:`_ring_layout` so
    it is one unbroken polygon).

    Each unit's own ``boxes_um["marker"]``/``boxes_um["tap"]`` (see
    :func:`_bjt_unit_layout`) are placed per cell by the caller (there is no
    array-level device-mark box here as of issue #432 -- a marker coincident
    with the whole shared well would make every diffusion pad inside it,
    base-tie pads included, look like a second emitter). The collector ring
    is still held :data:`BJT_COLLECTOR_GAP_UM` outside the well, well beyond
    gf180mcu's ``bjt.separation.comp.1`` 0.1um limit.
    """
    unit = _bjt_unit_layout(emitter_um)
    col_pitch = unit["total_len_um"] + MIN_SAME_LAYER_SPACING_UM
    row_pitch = unit["height_um"] + MIN_SAME_LAYER_SPACING_UM

    order = (
        _centroid_order(rows, cols)
        if topology == "common_centroid"
        else [(r, c) for r in range(rows) for c in range(cols)]
    )
    cells = [
        {"idx": idx, "row": r, "col": c, "x0_um": c * col_pitch, "y0_um": r * row_pitch}
        for idx, (r, c) in enumerate(order)
    ]

    dummy_cells = []
    for r in range(rows):
        for dc in range(1, dummy + 1):
            dummy_cells.append(
                {"row": r, "col": -dc, "x0_um": -dc * col_pitch, "y0_um": r * row_pitch}
            )
            dummy_cells.append(
                {
                    "row": r,
                    "col": cols - 1 + dc,
                    "x0_um": (cols - 1 + dc) * col_pitch,
                    "y0_um": r * row_pitch,
                }
            )

    all_cells = cells + dummy_cells
    min_x0 = min(c["x0_um"] for c in all_cells)
    max_x1 = max(c["x0_um"] + unit["total_len_um"] for c in all_cells)
    min_y0 = min(c["y0_um"] for c in all_cells)
    max_y1 = max(c["y0_um"] + unit["height_um"] for c in all_cells)

    margin = WELL_ENCLOSURE_MARGIN_UM
    well_box = (min_x0 - margin, min_y0 - margin, max_x1 + margin, max_y1 + margin)

    ring = None
    ring_offset = (0.0, 0.0)
    if add_collector_ring:
        gap = BJT_COLLECTOR_GAP_UM
        ring_w = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
        inner_x0 = well_box[0] - gap
        inner_y0 = well_box[1] - gap
        inner_w = (well_box[2] - well_box[0]) + 2 * gap
        inner_h = (well_box[3] - well_box[1]) + 2 * gap
        contacts_per_side = max(1, min(rows + cols, 4))
        ring = _ring_layout(
            inner_w,
            inner_h,
            ring_w,
            contacts_per_side,
            ring_gap_side,
            ring_gap_um,
            ring_gap_offset_um,
        )
        ring_offset = _snap_offset_um(inner_x0 - ring_w, inner_y0 - ring_w)

    return {
        "unit": unit,
        "cells": cells,
        "dummy_cells": dummy_cells,
        "col_pitch_um": col_pitch,
        "row_pitch_um": row_pitch,
        "well_box_um": well_box,
        "ring": ring,
        "ring_offset_um": ring_offset,
    }


def _esd_device_layout(
    finger_width_um: float,
    l_um: float,
    fingers: int,
    add_guard_ring: bool,
    ring_gap_side: str = "",
    ring_gap_um: float = 0.0,
    ring_gap_offset_um: float = 0.0,
    ring_padding_um: float = GUARD_RING_DEFAULT_PADDING_UM,
    gate_contact: bool = False,
    contact_gate_offset_um: float = 0.0,
    bottom_endcap_um: float = 0.0,
    sd_implant_margin_um: float = 0.0,
) -> dict[str, Any]:
    """Grounded-gate multi-finger ESD MOS clamp (issue #569): a single
    :func:`_mos_unit_layout` unit device with ``fingers`` gate stripes across
    one shared diffusion strip -- the standard ggNMOS ESD-clamp layout idiom
    -- optionally enclosed by an automatically-sized tap ring.

    Composes exactly the way :func:`_diff_pair_layout` composes families 1
    and 3: it calls :func:`_mos_unit_layout` (the same unit-device helper
    ``mos_array``/``diff_pair`` already use, here with its own ``fingers``
    parameter doing double duty as the ESD device's own multi-finger count)
    and :func:`_ring_layout` (the same ring helper ``guard_ring``/
    ``diff_pair``/``bjt_array`` already use) directly -- no second, private
    layout mechanism, and no sub-cell instantiation of ``_GuardRingPCell``
    itself (``diff_pair`` doesn't either; see that class's own docstring).

    ``contact_gate_offset_um``/``bottom_endcap_um``/``sd_implant_margin_um``
    (issue #1577) are forwarded to :func:`_mos_unit_layout` unchanged -- see
    that function's own docstring."""
    unit = _mos_unit_layout(
        finger_width_um,
        l_um,
        fingers,
        gate_contact,
        contact_gate_offset_um=contact_gate_offset_um,
        bottom_endcap_um=bottom_endcap_um,
        sd_implant_margin_um=sd_implant_margin_um,
    )

    ring = None
    ring_offset = (0.0, 0.0)
    if add_guard_ring:
        padding = ring_padding_um
        ring_w = GUARD_RING_DEFAULT_WIDTH_UM
        ring = _ring_layout(
            unit["total_len_um"] + 2 * padding,
            unit["bbox_height_um"] + 2 * padding,
            ring_w,
            GUARD_RING_DEFAULT_CONTACTS_PER_SIDE,
            ring_gap_side,
            ring_gap_um,
            ring_gap_offset_um,
        )
        ring_offset = _snap_offset_um(-(ring_w + padding), -(ring_w + padding))

    return {
        "unit": unit,
        "ring": ring,
        "ring_offset_um": ring_offset,
    }


def _shift_box(
    box_um: tuple[float, float, float, float], ox_um: float, oy_um: float
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box_um
    return (x0 + ox_um, y0 + oy_um, x1 + ox_um, y1 + oy_um)


def _insert_boxes(
    cell: Any,
    layer_index: int,
    dbu: float,
    boxes_um: list[tuple[float, float, float, float]],
    ox_um: float = 0.0,
    oy_um: float = 0.0,
) -> None:
    import klayout.db as kdb

    shapes = cell.shapes(layer_index)
    for x0, y0, x1, y1 in boxes_um:
        shapes.insert(
            kdb.Box(
                int(round((x0 + ox_um) / dbu)),
                int(round((y0 + oy_um) / dbu)),
                int(round((x1 + ox_um) / dbu)),
                int(round((y1 + oy_um) / dbu)),
            )
        )


def _insert_ring(
    cell: Any,
    layer_index: int,
    dbu: float,
    outer_box_um: tuple[float, float, float, float],
    inner_box_um: tuple[float, float, float, float],
    gap_box_um: tuple[float, float, float, float] | None = None,
) -> None:
    """Insert an outer-box-minus-inner-box ring as one boolean ``Region`` --
    guarantees a single unbroken polygon (no same-layer internal-space
    violation between "segments"), per :func:`_ring_layout`'s docstring.

    ``gap_box_um`` (#434), when given, is subtracted as well: it cuts one
    routing opening through the ring's band on a single side, leaving a
    C-shaped -- still connected, still one polygon -- conductor.
    """
    import klayout.db as kdb

    def _to_box(box_um: tuple[float, float, float, float]) -> Any:
        x0, y0, x1, y1 = box_um
        return kdb.Box(
            int(round(x0 / dbu)),
            int(round(y0 / dbu)),
            int(round(x1 / dbu)),
            int(round(y1 / dbu)),
        )

    ring = kdb.Region(_to_box(outer_box_um)) - kdb.Region(_to_box(inner_box_um))
    if gap_box_um is not None:
        ring = ring - kdb.Region(_to_box(gap_box_um))
    cell.shapes(layer_index).insert(ring)


class GenError(Exception):
    """Raised when a generation request cannot be fulfilled.

    Covers an unknown generator name, an unresolvable PDK, and invalid/
    out-of-range ``params`` -- the CLI turns this into a clean stderr
    message + exit code 1, never a traceback (see docs/cli/gen.md's exit
    code table).
    """


def list_generators() -> dict[str, Any]:
    """Enumerate the available generators (``klt gen --list``).

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/gen.md``)::

        {
            "schema_version": 1,
            "generators": [
                {
                    "name": str,
                    "summary": str,
                    "params": [
                        {
                            "name": str,
                            "type": "double" | "int" | "string" | "bool",
                            "default": <JSON value>,
                            "description": str,
                        },
                        ...
                    ],
                },
                ...
            ],
        }

    ``params`` documents every request-adjustable ``params`` field the
    generator's PCell declares (hidden, implementation-only parameters such
    as the drawing layer are omitted -- see ``_HIDDEN_PARAMS``).
    """
    pcell_classes = _build_pcell_classes()

    generators: list[dict[str, Any]] = []
    for name in sorted(_GENERATOR_SPECS):
        spec = _GENERATOR_SPECS[name]
        decl = pcell_classes[name]()
        params = [
            {
                "name": p.name,
                "type": _PARAM_TYPE_NAMES.get(p.type, "unknown"),
                "default": p.default,
                "description": p.description,
            }
            for p in decl.get_parameters()
            if p.name not in _HIDDEN_PARAMS
        ]
        generators.append(
            {"name": spec.name, "summary": spec.summary, "params": params}
        )

    return {"schema_version": SCHEMA_VERSION, "generators": generators}


def load_params_arg(value: str | None) -> dict[str, Any]:
    """Resolve a CLI ``--params`` value into a ``params`` dict.

    ``value`` is either a path to a JSON file (existing files win first) or
    an inline JSON object string, per docs/cli/gen.md. ``None`` (flag
    omitted) resolves to ``{}`` -- every generator's parameters have
    defaults, so an empty ``params`` object is a valid request.

    Raises :class:`GenError` if the value is neither a readable JSON file
    nor valid inline JSON, or decodes to something other than a JSON object.
    """
    import json

    if value is None:
        return {}

    if os.path.isfile(value):
        try:
            with open(value, encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError as exc:
            raise GenError(f"could not read params file '{value}': {exc}") from exc
        except json.JSONDecodeError as exc:
            raise GenError(f"params file '{value}' is not valid JSON: {exc}") from exc
    else:
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise GenError(
                "--params must be a path to a JSON file or an inline JSON "
                f"object: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise GenError("--params must decode to a JSON object")
    return data


def generate(request: dict[str, Any]) -> dict[str, Any]:
    """Run one generator request end-to-end and return the response envelope.

    ``request`` follows the ``klt.gen.request/1`` shape (spike section 2)::

        {
            "schema": "klt.gen.request/1",
            "generator": "resistor_strip",
            "pdk": {"variant": "sky130A", "root": None},
            "params": {"length_um": 2.0, "num": 4},
            "options": {"cell_name": "res_strip_0", "output": "res_strip_0.gds"},
        }

    ``pdk``/``params``/``options`` are all optional; missing ``pdk`` falls
    through to ``find_pdk()``'s own ``$PDK``/``$PDK_ROOT`` search (same
    behaviour as ``klt pdk find`` with no flags). Returns a dict matching
    the documented response schema (see ``docs/cli/gen.md``)::

        {
            "schema_version": 1,
            "generator": str,
            "cell_name": str,
            "gds_path": str,
            "pdk": {"name": str, "variant": str, "version": str | None},
            "dbu_um": float,
            "bbox_um": {"x0": float, "y0": float, "x1": float, "y1": float},
            "device_count": int,
            "ports": [...],
            "drc_hints": {...},
            "warnings": [...],
            "provenance": {...},
        }

    ``provenance`` (issue #2035) is the same shared block ``klt drc``/``klt
    lvs``/``klt extract``/``klt sta`` already emit, built via
    :func:`~klayout_tools._provenance.build_provenance` -- see
    ``docs/json-contract.md``'s "Shared ``provenance`` block". Its point
    here is ``klt_version``/``klayout_version``: a project that commits
    generated geometry *and this report* as evidence and regenerates later
    could otherwise not tell a real geometry change from a klt/KLayout
    upgrade. A generator request carries no rule/model deck and no single
    input layout stream (the request is parameters, not a layout file), so
    ``provenance.deck``/``provenance.input`` are always ``None`` -- matching
    ``klt lvs`` against a pre-extracted netlist. ``provenance.pdk`` carries
    the same identity as the top-level ``pdk`` field above, in the shared
    block's own ``{name, source, version}`` spelling. Additive field, no
    ``schema_version`` bump.

    ``dbu_um`` (issue #1496) reports the database unit the output stream was
    actually written at -- resolved from the *resolved PDK's own tech LEF*
    (``DATABASE MICRONS``) by :func:`~klayout_tools.pdk.resolve_pdk_dbu`, not
    a fixed ``0.001`` -- so a caller can confirm two blocks agree before
    handing them to ``klt gen-compose`` (which requires one shared dbu across
    every block) without a separate ``klt stats`` round-trip. Additive field,
    no ``schema_version`` bump (see ``docs/json-contract.md``).

    ``navigable_regions`` (issue #1531, Phase 1) reports metal-free
    rectangles ``{"x0_um", "y0_um", "x1_um", "y1_um"}`` inside the emitted
    cell's own footprint that a router could pass through -- currently
    populated only by ``mos_array``'s ``interior_channel_um`` param (see
    :func:`_mos_array_layout`); every other generator, and ``mos_array``
    itself at its default ``interior_channel_um=0.0``, reports an empty
    list. Another additive field, no ``schema_version`` bump: a future
    `gen-compose` phase is expected to subtract these from a block's
    obstacle bbox (not implemented by this phase).

    Raises :class:`GenError` for an unknown generator, an unresolvable PDK,
    invalid/out-of-range ``params``, or a write failure (e.g. the
    ``options.output`` directory does not exist).
    """
    if not isinstance(request, dict):
        raise GenError("request must be a JSON object")

    generator_name = request.get("generator")
    if not isinstance(generator_name, str) or not generator_name:
        raise GenError("request.generator is required")

    spec = _GENERATOR_SPECS.get(generator_name)
    if spec is None:
        raise GenError(
            f"unknown generator '{generator_name}' -- available: "
            f"{', '.join(sorted(_GENERATOR_SPECS))} (see `klt gen --list`)"
        )

    pdk_request = request.get("pdk") or {}
    if not isinstance(pdk_request, dict):
        raise GenError("request.pdk must be a JSON object")
    try:
        pdk_info = find_pdk(
            variant=pdk_request.get("variant"), root=pdk_request.get("root")
        )
    except PdkNotFoundError as exc:
        raise GenError(str(exc)) from exc

    raw_params = request.get("params") or {}
    if not isinstance(raw_params, dict):
        raise GenError("request.params must be a JSON object")

    options = request.get("options") or {}
    if not isinstance(options, dict):
        raise GenError("request.options must be a JSON object")
    cell_name = options.get("cell_name") or f"{generator_name}_0"
    output_path = options.get("output") or f"{cell_name}.gds"

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir and not os.path.isdir(output_dir):
        raise GenError(f"output directory does not exist: {output_dir}")

    resolved_params = _resolve_params(spec, raw_params)
    spec.validate(resolved_params)

    layout, top_cell = _produce(spec, cell_name, resolved_params, pdk_info)

    write_layout(layout, output_path, GenError)

    dbu = layout.dbu
    bbox = top_cell.bbox()
    bbox_um = {
        "x0": bbox.left * dbu,
        "y0": bbox.bottom * dbu,
        "x1": bbox.right * dbu,
        "y1": bbox.top * dbu,
    }

    described = spec.describe(resolved_params, dbu, pdk_info)

    return {
        "schema_version": SCHEMA_VERSION,
        "generator": generator_name,
        "cell_name": cell_name,
        "gds_path": output_path,
        "pdk": {
            "name": pdk_info["variant"],
            "variant": pdk_info["variant"],
            "version": pdk_info["version"],
        },
        "dbu_um": dbu,
        "bbox_um": bbox_um,
        "device_count": described["device_count"],
        "ports": described["ports"],
        "navigable_regions": described.get("navigable_regions", []),
        "drc_hints": described["drc_hints"],
        "warnings": described["warnings"],
        # Issue #2035: the shared `provenance` block every other verb's
        # report already carries -- see this function's docstring. A `gen`
        # request involves no rule/model deck and no single input layout
        # stream, so `deck`/`input` are both `None` here.
        "provenance": build_provenance(pdk=pdk_info),
    }


# --------------------------------------------------------------------------- #
# PCell harness -- adapts a request's `params` to a PCellDeclarationHelper
# subclass's declared parameter schema + produce_impl(), per spike section 1's
# "KLayout PCells (native)" entry. Generic across every registered generator;
# generator-specific knowledge (validation, port/bbox reporting) lives in each
# _GeneratorSpec, not here.
# --------------------------------------------------------------------------- #


def _resolve_params(spec: _GeneratorSpec, raw_params: dict[str, Any]) -> dict[str, Any]:
    """Merge ``raw_params`` onto the generator's declared PCell defaults.

    Every declared, non-hidden parameter ends up in the result: the request's
    value when given (type-checked against the PCell's declared type), the
    PCell's own default otherwise. Raises :class:`GenError` for an unknown
    parameter name or a value that doesn't match the declared type.
    """
    pcell_classes = _build_pcell_classes()
    decl = pcell_classes[spec.name]()
    declared = {
        p.name: p for p in decl.get_parameters() if p.name not in _HIDDEN_PARAMS
    }

    unknown = sorted(set(raw_params) - set(declared))
    if unknown:
        raise GenError(f"generator '{spec.name}': unknown params: {', '.join(unknown)}")

    resolved: dict[str, Any] = {}
    for name, pdecl in declared.items():
        if name in raw_params:
            resolved[name] = _coerce_param(
                spec.name, name, raw_params[name], pdecl.type
            )
        else:
            resolved[name] = pdecl.default
    return resolved


def _coerce_param(generator: str, name: str, value: Any, ptype: int) -> Any:
    import klayout.db as kdb

    Decl = kdb.PCellParameterDeclaration
    if ptype == Decl.TypeDouble:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GenError(f"generator '{generator}': params.{name} must be a number")
        return float(value)
    if ptype == Decl.TypeInt:
        if isinstance(value, bool) or not isinstance(value, int):
            raise GenError(f"generator '{generator}': params.{name} must be an integer")
        return int(value)
    if ptype == Decl.TypeString:
        if not isinstance(value, str):
            raise GenError(f"generator '{generator}': params.{name} must be a string")
        return value
    if ptype == Decl.TypeBoolean:
        if not isinstance(value, bool):
            raise GenError(f"generator '{generator}': params.{name} must be a boolean")
        return value
    if ptype == Decl.TypeList:
        # A JSON array passes through as a plain Python list -- KLayout's
        # PCell variant machinery round-trips nested lists unchanged (see
        # `_WellIslandPCell.isolate_from`). Element *shape* is the individual
        # generator's business, validated in its own `validate()` (e.g.
        # :func:`_parse_well_regions`), the same division of labour every
        # other param type here follows: this function only enforces the
        # declared JSON type.
        if not isinstance(value, list):
            raise GenError(f"generator '{generator}': params.{name} must be an array")
        return list(value)
    raise GenError(
        f"generator '{generator}': params.{name} has an unsupported PCell "
        "parameter type"
    )


def _produce(
    spec: _GeneratorSpec,
    cell_name: str,
    resolved_params: dict[str, Any],
    pdk_info: dict[str, Any],
) -> tuple[kdb.Layout, kdb.Cell]:
    """Instantiate ``spec``'s PCell into a fresh layout as cell ``cell_name``.

    This is the harness's one KLayout-specific step: register (once per
    process) a ``pya.Library`` wrapping every reference generator's PCell
    declaration, then use ``Layout.add_pcell_variant`` -- the same mechanism
    KLayout's own GUI PCell panel uses -- to build geometry headlessly.

    ``pdk_info`` (the resolved request's ``pdk`` payload, from
    :func:`~klayout_tools.pdk.find_pdk`) is threaded through to
    ``spec.layer_params`` so a PDK-aware generator (every phase-2 generator
    except ``resistor_strip``) can resolve its hidden layer params against
    the *resolved* PDK family rather than a fixed default -- see
    :func:`_device_layer_params` and friends.

    The output layout's dbu is resolved from ``pdk_info``'s own tech LEF via
    :func:`~klayout_tools.pdk.resolve_pdk_dbu` (issue #1496) -- e.g. 0.0005
    for gf180mcu's ``DATABASE MICRONS 2000`` -- so a ``klt gen`` output
    agrees, by construction, with a ``klt place_and_route`` output resolved
    against the same PDK (both derive their dbu from the same tech LEF, the
    way `place_and_route`'s own DEF/GDS merge already does per issue #1032).
    Falls back to ``spec.dbu`` (the generator's hardcoded default, historically
    always 0.001) when the resolver returns ``None`` -- a PDK whose tech LEF
    is missing or unparsable never fails a `klt gen` request that previously
    succeeded, it just keeps today's hardcoded dbu.
    """
    import klayout.db as kdb

    lib = _pcell_library()
    decl = lib.layout().pcell_declaration(spec.name)

    pcell_values = dict(resolved_params)
    pcell_values.update(spec.layer_params(pdk_info, resolved_params))

    layout = kdb.Layout()
    layout.dbu = resolve_pdk_dbu(pdk_info) or spec.dbu
    pcell_var = layout.add_pcell_variant(lib, decl.id(), pcell_values)
    top = layout.create_cell(cell_name)
    top.insert(kdb.CellInstArray(pcell_var, kdb.Trans()))
    return layout, top


def _pcell_library() -> kdb.Library:
    """Return the reference PCell library, registering it on first use.

    ``pya.Library.register()`` is process-global -- registering twice under
    the same name would either error or shadow the first registration
    depending on KLayout version, so this guards with ``library_by_name()``
    and registers exactly once per process, mirroring how a real PDK's
    KLayout tech file registers its own PCell libraries on load.
    """
    import klayout.db as kdb

    existing = kdb.Library.library_by_name(_PCELL_LIBRARY_NAME)
    if existing is not None:
        return existing

    pcell_classes = _build_pcell_classes()

    class _ReferenceLibrary(kdb.Library):
        def __init__(self) -> None:
            super().__init__()
            self.description = "klt gen reference PCell library (phase 1 skeleton)"
            for name, pcell_cls in pcell_classes.items():
                self.layout().register_pcell(name, pcell_cls())
            self.register(_PCELL_LIBRARY_NAME)

    return _ReferenceLibrary()


def _build_pcell_classes() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Assemble every reference generator's ``PCellDeclarationHelper``
    subclass into one name -> class mapping.

    Thin composer over the per-family ``_build_<family>_pcell()`` factory
    functions, relocated (issue #1698) into their own submodules under
    ``klayout_tools.gen_pcells`` -- see that package's docstring for the
    rationale. Each factory still does its own local
    ``import klayout.db as kdb`` so no factory pays the import cost until
    it's actually called, and this function's own ``dict[str, type[...]]``
    return shape is unchanged from before the split.
    """
    pcell_classes: dict[str, type[kdb.PCellDeclarationHelper]] = {}
    pcell_classes.update(_build_resistor_strip_pcell())
    pcell_classes.update(_build_mos_array_pcell())
    pcell_classes.update(_build_res_array_pcell())
    pcell_classes.update(_build_cap_array_pcell())
    pcell_classes.update(_build_guard_ring_pcell())
    pcell_classes.update(_build_well_island_pcell())
    pcell_classes.update(_build_diff_pair_pcell())
    pcell_classes.update(_build_bjt_array_pcell())
    pcell_classes.update(_build_bond_pad_pcell())
    pcell_classes.update(_build_esd_device_pcell())
    return pcell_classes


# --------------------------------------------------------------------------- #
# Reference generator registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _GeneratorSpec:
    """Generator-specific knowledge the generic harness (:func:`_produce`)
    doesn't have: sanity-check bounds, how to describe the produced
    geometry as the response envelope's ``device_count``/``ports``/
    ``drc_hints`` fields, and how to resolve its hidden layer params against
    a resolved PDK (see :func:`_device_layer_params` and friends).
    """

    name: str
    summary: str
    #: Fallback output database unit (um) -- used only when the resolved
    #: PDK's own tech LEF declares no ``DATABASE MICRONS`` value (or ships
    #: no readable tech LEF at all), since :func:`_produce` prefers
    #: :func:`~klayout_tools.pdk.resolve_pdk_dbu`'s PDK-derived answer
    #: (issue #1496). Every registered generator declares ``0.001``, the dbu
    #: `klt gen` unconditionally wrote at before that change.
    dbu: float
    validate: Callable[[dict[str, Any]], None]
    describe: Callable[[dict[str, Any], float, dict[str, Any]], dict[str, Any]]
    layer_params: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


#: Maps ``PCellParameterDeclaration`` type constants to the JSON type names
#: reported by ``klt gen --list``.
_PARAM_TYPE_NAMES = {
    0: "int",
    1: "double",
    2: "string",
    3: "bool",
    6: "list",  # PCellParameterDeclaration.TypeList -- a JSON array
}

_GENERATOR_SPECS: dict[str, _GeneratorSpec] = {
    "resistor_strip": _GeneratorSpec(
        name="resistor_strip",
        summary=(
            "Row of parametrized rectangles standing in for a unit-resistor "
            "string -- the phase-1 reference generator proving the request/"
            "response contract end-to-end. Not DRC-clean; phase 2 replaces "
            "this with a real resistor-array generator "
            "(docs/design/layout-generator-spike.md section 4.2)."
        ),
        dbu=0.001,
        validate=_resistor_strip_validate,
        describe=_resistor_strip_describe,
        layer_params=_resistor_strip_layer_params,
    ),
    "mos_array": _GeneratorSpec(
        name="mos_array",
        summary=(
            "Matched MOS transistor array: identical unit devices (active + "
            "poly gate; contact + local-metal landing pads on the S/D "
            "terminals, gate exposed as bare poly unless params.gate_contact "
            "finishes its stack too) placed on a "
            "uniform grid, with optional dummy columns at each end and a "
            "centroid-symmetric port-numbering order for common-centroid "
            "matching -- family 1 of the analog primitive generators "
            "(docs/design/layout-generator-spike.md section 4)."
        ),
        dbu=0.001,
        validate=_mos_array_validate,
        describe=_mos_array_describe,
        layer_params=_mos_array_layer_params,
    ),
    "res_array": _GeneratorSpec(
        name="res_array",
        summary=(
            "Unit resistor/capacitor array: a row of matched unit elements "
            "(poly body + contact + local-metal pads at both ends) with "
            "dummy elements at each end, per the sky130-bandgap-reference KB "
            "entry's resistor-array layout idiom -- family 2. "
            "params.metal_level (1..5 on sky130, 1..2 on sg13g2, 1..3 on "
            "gf180mcu) instead draws that level's drawn metal-layer "
            "resistor (sky130's met1..met5 res_generic_mN, issue #1639; "
            "sg13g2's Metal1/Metal2 res_metal1/res_metal2, issue #1758; "
            "gf180mcu's Metal1/Metal2/Metal3 rm1/rm2/rm3, issue #1731) -- "
            "body + resistor-ID marker + an end via/landing-pad stack to "
            "an adjacent metal level (down on sky130, up on sg13g2/"
            "gf180mcu -- see _PDK_METAL_RES_LEVELS's own docstring)."
        ),
        dbu=0.001,
        validate=_res_array_validate,
        describe=_res_array_describe,
        layer_params=_resistor_layer_params,
    ),
    "cap_array": _GeneratorSpec(
        name="cap_array",
        summary=(
            "Unit MiM capacitor array: a row of matched unit cells, each a "
            "top-plate-metal-over-bottom-plate-metal MiM stack (sky130's "
            "capm/met3) with a top-plate via + local-metal landing pad -- "
            "the capacitor sibling of res_array (issue #1117)."
        ),
        dbu=0.001,
        validate=_cap_array_validate,
        describe=_cap_array_describe,
        layer_params=_cap_array_layer_params,
    ),
    "guard_ring": _GeneratorSpec(
        name="guard_ring",
        summary=(
            "Substrate/well tap guard ring: a tap ring with evenly-spaced "
            "contacts and a local-metal ring, optionally enclosed by a well "
            "tie on PDK families whose curated deck checks one -- family 3."
        ),
        dbu=0.001,
        validate=_guard_ring_validate,
        describe=_guard_ring_describe,
        layer_params=_ring_layer_params,
    ),
    "well_island": _GeneratorSpec(
        name="well_island",
        summary=(
            "Named-net, isolated well/tap island: guard_ring's tap ring + "
            "well tie, with the tie bound to a caller-supplied net (drawn as "
            "a label on the ring's own metal, never on the well label layer) "
            "and a separation constraint against a caller-supplied set of "
            "other well regions -- sized to clear them or rejected outright, "
            "never silently merged. The primitive for giving two groups of "
            "same-flavour devices two different body potentials (issue "
            "#1421)."
        ),
        dbu=0.001,
        validate=_well_island_validate,
        describe=_well_island_describe,
        layer_params=_well_island_layer_params,
    ),
    "diff_pair": _GeneratorSpec(
        name="diff_pair",
        summary=(
            "Differential pair / current mirror cell: two matched devices "
            "(Q1/Q2, or M1/M2 with params.mirror) split into params.splits "
            "sub-instances each and interleaved in a true common-centroid "
            "cross-quad pattern, optionally enclosed by an automatically-"
            "sized guard ring -- family 4, composing families 1 and 3. The "
            "interleave puts one Q1 and one Q2 sub-instance in every column, "
            "so the two legs share one x column per terminal -- read "
            "docs/cli/gen.md's diff_pair section before floorplanning a "
            "one-column-per-net routing grid around it."
        ),
        dbu=0.001,
        validate=_diff_pair_validate,
        describe=_diff_pair_describe,
        layer_params=_diff_pair_layer_params,
    ),
    "bjt_array": _GeneratorSpec(
        name="bjt_array",
        summary=(
            "Matched vertical-bipolar (PNP/BJT) array: a common-centroid grid "
            "of identical unit devices (emitter + base-tie diffusion with "
            "contacts and local metal) sharing one base well, enclosed by a "
            "collector guard ring and a bipolar device-mark layer on PDK "
            "families whose curated deck checks one (gf180mcu's DRC_BJT) -- "
            "Epic #152 phase 4, drawn from base layers per "
            "docs/design/gen-bjt-array-spike.md."
        ),
        dbu=0.001,
        validate=_bjt_array_validate,
        describe=_bjt_array_describe,
        layer_params=_bjt_layer_params,
    ),
    "bond_pad": _GeneratorSpec(
        name="bond_pad",
        summary=(
            "Chip-boundary bond pad: a passivation opening enclosed by the "
            "resolved PDK family's own topmost routing metal, satisfying "
            "gf180mcu's only hard bond-pad DRC rule (PAD.4) -- the first "
            "generator in this family covering the I/O boundary rather than "
            "a core analog device (issue #568)."
        ),
        dbu=0.001,
        validate=_bond_pad_validate,
        describe=_bond_pad_describe,
        layer_params=_bond_pad_layer_params,
    ),
    "esd_device": _GeneratorSpec(
        name="esd_device",
        summary=(
            "Grounded-gate multi-finger ESD protection MOS: one multi-finger "
            "unit device (params.fingers gate stripes across a shared "
            "diffusion, the standard ggNMOS ESD-clamp layout idiom), "
            "optionally enclosed in an automatically-sized tap ring composing "
            "guard_ring's own ring-drawing helper (family 3), the same "
            "composition mechanism diff_pair already uses for families 1 and "
            "3, with an optional salicide_block layer -- issue #569."
        ),
        dbu=0.001,
        validate=_esd_device_validate,
        describe=_esd_device_describe,
        layer_params=_esd_device_layer_params,
    ),
}
