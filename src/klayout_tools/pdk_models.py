"""Curated MOS device-model binding table + SPICE writer delegate for
`klt extract --pdk ...` (see ``docs/cli/extract.md`` and issue #209).

**Problem this solves**: `klt extract`'s default (no ``--pdk``) SPICE output
writes each extracted MOS device as a bare ``M`` card whose model name is the
curated deck's own device-class label (``nfet``/``pfet``, see
``klayout_tools.decks.ExtractionDeck``) -- that label does not correspond to
any model a real PDK ships, so the netlist cannot bind the real PDK model
library at all (neither sky130 nor gf180mcu defines a ``.model nfet``; both
ship the real primitive device as a ``.subckt``, not a built-in ``nmos``/
``pmos`` model -- see the module docstring's "Card shape" note below). When
``klt extract`` is given ``--pdk``/``--pdk-root`` and the PDK resolves, this
module supplies the pieces that rewrite each MOS device onto the resolved
PDK's real device library instead: a small curated
``(deck_name, pdk_variant_family) -> {"nfet": ..., "pfet": ...}`` lookup
table (:func:`resolve_mos_model_table`) and a
``kdb.NetlistSpiceWriterDelegate`` subclass
(:func:`create_model_binding_delegate`) that emits an ``X`` subcircuit call
per bound device instead of KLayout's default ``M`` card.

**Scope** (deliberately narrower than the general PDK-device-metadata
resolver ``docs/design/pdk-device-corner-metadata-spike.md`` proposes as a
follow-up epic -- see issue #209's Curator enhancement). As of issue #339 the
binding covers, per PDK family (``sky130``, ``gf180mcu``, and -- MOS only, as
of issue #1231 -- ``sg13g2``; MOS only, as of issue #1400 -- ``sg13cmos5l``):

- **MOS** (all four decks) -- the deck's default voltage flavor per family,
  plus (issue #1111 for gf180mcu, issue #1231 for sg13g2, issue #1369 for
  sky130) any additional marker-scoped flavor its
  ``ExtractionDeck.mos_flavours`` declares (``nfet_06v0``/``pfet_06v0`` for a
  transistor recognised inside ``Dualgate``; ``sg13_hv_nmos``/
  ``sg13_hv_pmos`` for one inside ``ThickGateOx``;
  ``sky130_fd_pr__nfet_g5v0d10v5``/``sky130_fd_pr__pfet_g5v0d10v5`` for one
  inside sky130's ``hvi``) -- see :data:`MOS_FLAVOUR_PROPERTY` and
  :data:`_MOS_MODEL_FLAVOURS` below, and ``klayout_tools.extract``'s module
  docstring.
- **Resistor** (sky130/gf180mcu, plus -- issue #1457 -- sg13g2's three drawn
  poly flavours) -- every ``ResistorDevice`` class each deck declares
  (:func:`resolve_device_bindings` reads the deck's own list) that has a real
  subcircuit to bind to. sg13g2's ``rsil``/``rppd``/``rhigh`` (issues
  #1231/#1235) bind to the real 3-terminal ``rsil``/``rppd``/``rhigh``
  subcircuits. sg13g2's two drawn *metal* resistors (``res_metal1``/
  ``res_metal2``, issue #1235) are a **deliberate, verified carve-out**, not
  a "not implemented yet" gap: a real fetched IHP-Open-PDK v0.3.0 install
  defines no ``.subckt``/``.model`` for either name at all (IHP's own
  reference CDL netlists instantiate them as a bare
  semiconductor-resistor-with-model-reference card that a consumer's own
  simulation deck must supply the ``.model`` for) -- the same documented
  bare-primitive carve-out gf180mcu's bipolar gets below.
- **Capacitor** (sky130/gf180mcu) -- every ``CapacitorDevice`` class each deck
  declares, with plate ``L``/``W`` derived from the extracted plate area and
  perimeter via :func:`equivalent_rectangle_um`. sg13g2's two MIM capacitors
  (``cap_cmim``/``rfcmim``, issue #1454) have no curated entry yet and keep
  their bare ``C``-card form -- the same documented bare-primitive carve-out
  that deck's own poly resistors get above.
- **Bipolar** -- **sky130 only** (``sky130_fd_pr__pnp_05v5``). gf180mcu's
  bipolar is deliberately left **unbound** (its recognised ``bjt`` device
  keeps KLayout's bare ``Q``-card form), because the gf180mcu deck itself has
  no positively-identified single device-cell name to bind against -- an
  existing, already-documented data gap in that deck
  (``decks/gf180mcu.py:557-559``), not something this binding can resolve by
  guessing a subcircuit name. See ``docs/cli/extract.md`` -> "SPICE model
  binding" -> "Scope limits".

Any recognised device class with no curated binding entry is written as its
bare primitive card (the pre-``--pdk`` form), never a guessed subcircuit call
-- the gf180mcu-bipolar carve-out above is the only such case today.

**The two directions are not symmetric** (issue #1464). The scope above is the
*writing* direction (``klt extract --pdk``, :func:`resolve_device_bindings`),
where a missing curated entry must fall back to the bare primitive card rather
than guess. The *ingestion* direction (``klt lvs``'s
``reference.form: "subckt-call"`` converter, :func:`build_device_binding_map`)
additionally derives an assumed-identity binding -- declared LVS device-class
name taken as its own subcircuit name -- for each ``resistors``/``capacitors``
class of a deck whose corresponding family table has no curated entry at all,
so ``sg13cmos5l``'s ``rsil``/``rppd``/``rhigh`` and ``sg13g2``'s
``cap_cmim``/``rfcmim`` above are readable back without a hand-written
``reference.device_map``. Guessing is safe there and unsafe here: the worst
case reading is a failed or rejected conversion of a name no PDK ships, while
the worst case writing is a shipped netlist that binds a subcircuit that does
not exist. See :func:`build_device_binding_map` for that fallback's three
guard rails.

This table is intentionally a single small module, not scattered inline
literals, so a future ``klt pdk device`` resolver can absorb/replace it
without touching the delegate/writer plumbing.

**Card shape**: sky130 and gf180mcu both ship their primitive MOS device as a
SPICE ``.subckt`` (taking ``d g s b`` terminals plus ``l``/``w`` geometry
params), never a built-in ``nmos``/``pmos`` model -- so binding a real model
means switching the *card shape* from ``M`` (built-in device) to ``X``
(subcircuit call), not just renaming the ``M`` card's model field (KLayout's
``NetlistSpiceWriter`` only ever writes the ``M``-card shape for a
``DeviceClassMOS4Transistor`` device; ``NetlistSpiceWriterDelegate`` --
confirmed directly against the installed ``klayout.db`` module -- is
KLayout's documented escape hatch for "using subcircuits rather than the
built-in devices").

**Subcircuit names + parameter convention, verified against real fetched PDK
installs** (not guessed from this issue's own suggested names):

- ``sky130_fd_pr__nfet_01v8`` / ``sky130_fd_pr__pfet_01v8`` -- confirmed via
  ``scripts/fetch-pdks.sh``'s lambdapdk mirror, whose real SkyWater sky130
  SRAM macro netlist
  (``pdks/lambdapdk/lambdapdk/sky130/libs/sky130sram/sky130_sram_1rw1r_64x256_8/spice/sky130_sram_1rw1r_64x256_8.sp``)
  instantiates both subcircuits directly, e.g.
  ``X1000 a_511_725# a_n8_115# VDD VDD sky130_fd_pr__pfet_01v8 W=3 L=0.15`` --
  confirming both the subcircuit name and a ``d g s b <model> W=... L=...``
  terminal order matching KLayout's own default ``M``-card terminal order
  (see below).
- ``nfet_03v3`` / ``pfet_03v3`` (gf180mcu's 3.3V core-voltage flavor,
  gf180mcu's analogue to sky130's "01v8" designation -- gf180mcu has no
  ``gf180mcu_fd_pr__`` naming prefix convention the way sky130 does; verified
  by grepping the real, no-prefix subcircuit names throughout the installed
  tree) -- confirmed via a real gf180mcuA install (volare,
  ``~/.volare/gf180mcuA/libs.tech/ngspice/sm141064.ngspice``, ``.subckt
  nfet_03v3 d g s b w=1e-5 l=2.8e-7 ...``) and a real analog-IP SPICE view
  instantiating it
  (``~/.volare/gf180mcuA/libs.ref/gf180mcu_fd_io/spice/gf180mcu_fd_io.spice``,
  e.g. ``X5 n56 IE VSS VSS nfet_06v0 m=1.0 w=1.5e-6 l=700e-9 ...`` for the
  sibling 6V flavor -- same subcircuit family, confirming the no-prefix
  naming and the ``d g s b <model>`` terminal order). gf180mcu's real
  in-the-wild geometry values are written in raw SI (metres, e.g.
  ``w=1.5e-6``), never bare micrometre literals -- unlike the sky130 SRAM
  netlist's bare-micrometre convention above. Each family's geometry-literal
  convention is therefore written out per family; see
  ``_GEOMETRY_STYLE_BY_FAMILY`` below for the (verified, ngspice-reproduced)
  reason a unit-suffixed literal is *not* portable across the two.

Both real installs instantiate their MOS subcircuit with only ``l``/``w``
supplied -- no ``nf``/``mult``/``par`` override at the call site -- relying
on the subcircuit's own defaults (confirmed ``nf=1``/``par=1``-equivalent in
both fetched installs). The curated extraction decks' device extractor never
models multi-finger/multiplied devices either (one flat
``DeviceExtractorMOS4Transistor`` device per drawn gate -- see
``klayout_tools.extract``'s module docstring), so
:func:`create_model_binding_delegate` matches this real-world convention and
the extractor's own scope by emitting only ``L``/``W`` and omitting
``nf``/``mult``/``par`` (left at the resolved subcircuit's own default of 1).

Source/drain area+perimeter (``AS``/``AD``/``PS``/``PD``, present on the bare
``M``-card form) **are** carried onto the ``X`` card (issue #695): both
``sky130_fd_pr__nfet_01v8``/``pfet_01v8`` and ``nfet_03v3``/``pfet_03v3``
declare ``as``/``ad``/``ps``/``pd`` call-site parameters (confirmed in the
same real fetched installs, both defaulting to ``0`` -- e.g. gf180mcu's
``.subckt nfet_03v3 d g s b w=1e-5 l=2.8e-7 + as=0 ad=0 ps=0 pd=0 ...``), so
leaving them off the ``X`` card silently zeroed every bound MOS device's
source/drain junction area and perimeter -- and with it, its junction
capacitance -- even though the extractor measured the real, non-zero drawn
values right there on the unbound card. ``write_device`` reads them the same
way it reads ``L``/``W`` and writes them under the same (KLayout-native,
uppercase) parameter spelling the unbound ``M``-card form already uses --
SPICE subcircuit parameter names are matched case-insensitively by both
PDKs' real ngspice decks, so this does not need to track each PDK's own
lowercase spelling the way ``length_param``/``width_param`` do for
resistor/capacitor. Under the ``"unit_suffix"`` geometry style (gf180mcu, and
the unverified-family default -- see ``_GEOMETRY_STYLE_BY_FAMILY``) areas are
formatted with an explicit ``P`` (pico) unit suffix rather than ``U``
(micro) -- a source/drain area in square micrometres is numerically identical
to the same value in square metres times ``1e-12`` (e.g. ``0.8`` um²
``== 0.8e-12`` m² ``== 0.8P``), matching KLayout's own default ``M``-card
writer's formatting of ``AS``/``AD`` exactly. Under the ``"bare_um"`` style
(sky130) the *same* square-micrometre number is written with no suffix at
all, because that deck's ambient ``.option scale=1.0u`` already supplies the
unit (see ``docs/cli/extract.md``'s "SPICE model binding" section for a
worked ``M``-card/``X``-card example).

**Resistor / capacitor / bipolar subcircuit names + parameter conventions,
verified against the same real fetched PDK installs** (issue #339; every name
and parameter spelling below was read off a real install's own ``.subckt``
definition and a real in-the-wild instantiation, not transcribed from a deck
comment):

- **sky130 resistors** -- ``sky130_fd_pr__res_generic_po`` (two terminals
  ``r0 r1``), ``sky130_fd_pr__res_high_po`` / ``sky130_fd_pr__res_xhigh_po``
  (three terminals ``r0 r1 b``, the bulk tie), all geometry-parameterized by
  ``l``/``w`` in micrometres. Confirmed in
  ``~/.volare/sky130A/libs.tech/combined/continuous/models_resistors.spice``
  (``.subckt  sky130_fd_pr__res_high_po r0 r1 b mult=1`` + ``w=1 l=1``) and a
  real device-model instantiation
  (``sky130_fd_pr__res_high_po_2p85.model.spice``:
  ``x0 r0 r1 sub sky130_fd_pr__res_high_po l=l w=2.85 mult=mult``). The deck's
  ``bulk_to_substrate`` flag matches the two-vs-three-terminal split exactly
  (``res_generic_po`` is two-terminal, the ``rpm``/``urpm`` flavours carry the
  bulk tie).
- **gf180mcu resistors** -- ``ppolyf_u`` / ``ppolyf_u_1k`` / ``ppolyf_u_2k`` /
  ``ppolyf_u_3k`` (three terminals),
  geometry-parameterized by ``r_length``/``r_width`` (**not** ``l``/``w``) in
  metres, no ``gf180mcu_fd_pr__`` name prefix (same no-prefix convention the
  MOS table's ``nfet_03v3`` uses). Confirmed in
  ``~/.volare/gf180mcuA/libs.tech/ngspice/sm141064.ngspice``
  (``.subckt ppolyf_u 1 2 3 r_length=l r_width=w dtemp=0 par=1 s=1``, plus
  ``.subckt ppolyf_u_1k 1 2 3 r_length=l r_width=w ...`` and its ``_2k`` /
  ``_3k`` siblings on the identical terminal/parameter convention -- all
  three high-sheet-rho flavours are real, separately-modelled subcircuits,
  which is why ``--deck-option poly_res=2k`` can bind one) and a
  real analog-IP instantiation
  (``... ppolyf_u r_width=800e-9 r_length=1.6e-6 m=1.0 r=907.859 par=1``). The
  per-family parameter-name difference (``l``/``w`` vs ``r_length``/
  ``r_width``) is why the binding carries the subcircuit's own length/width
  parameter names per family rather than assuming one convention.
- **sg13g2 resistors** -- ``rsil`` / ``rppd`` / ``rhigh`` (issue #1457), each
  a three-terminal ``1 2 bn`` subcircuit (``bn`` the bulk/substrate tie,
  matching the deck's own ``bulk_to_substrate=True`` on all three classes),
  geometry-parameterized by ``l``/``w`` in raw metres (same convention as
  gf180mcu's resistors, but sky130-style parameter *names*). Confirmed in a
  real fetched IHP-Open-PDK v0.3.0 install's
  ``libs.tech/ngspice/models/resistors_mod.lib`` (``.subckt rsil 1 2 bn`` +
  ``.param w=0.5e-6 l=0.5e-6 ...``, and the ``rppd``/``rhigh`` siblings on the
  identical terminal/parameter convention with their own default
  ``w``/``l``). sg13g2's two drawn *metal* resistors (``res_metal1``/
  ``res_metal2``) have **no** curated entry: the same fetched install defines
  no ``.subckt``/``.model`` for either name anywhere under ``libs.tech/
  ngspice/`` or ``libs.tech/xyce/`` -- there is no real subcircuit to bind to,
  a verified carve-out rather than an unimplemented one (see the module
  docstring's "Scope" section above and ``docs/cli/extract.md``'s "Scope
  limits").
- **sky130 capacitors** -- the deck's LVS device names
  (``sky130_fd_pr__model__cap_mim`` on ``capm``/met3,
  ``sky130_fd_pr__model__cap_mim_m4`` on ``capm2``/met4) map to the
  *simulation* subcircuits ``sky130_fd_pr__cap_mim_m3_1`` and
  ``sky130_fd_pr__cap_mim_m3_2`` respectively (the ``_1``/``_2`` suffix is the
  lower/upper MiM stack), geometry-parameterized by ``w``/``l`` in
  micrometres. Confirmed in
  ``~/.volare/sky130A/libs.ref/sky130_fd_pr/spice/sky130_fd_pr__cap_mim_m3_1.model.spice``
  (``.subckt  sky130_fd_pr__cap_mim_m3_1 c0 c1 w=1 l=1 mf=1``).
- **gf180mcu capacitor** -- ``cap_mim_2f0_m4m5_noshield`` (the deck name is
  already the real subcircuit name), geometry-parameterized by
  ``c_length``/``c_width`` in metres. Confirmed in
  ``~/.volare/gf180mcuA/libs.tech/ngspice/sm141064_mim.ngspice``
  (``.subckt cap_mim_2f0_m4m5_noshield  1 2  c_length=l  c_width=w dtemp=0
  par=1``). KLayout's capacitor extractor exposes the plate ``A``/``P``
  (area/perimeter) but not ``L``/``W``, so the writer solves the same
  equivalent-rectangle quadratic ``extract.py``'s ``_n_squares`` already uses
  (factored into :func:`equivalent_rectangle_um`) to recover ``l``/``w`` for
  these geometry-parameterized subcircuits.
- **sky130 bipolar** -- ``sky130_fd_pr__pnp_05v5`` ships as a **geometry-named
  family**, not one parameterized cell: only two discrete emitter sizes exist,
  ``sky130_fd_pr__pnp_05v5_W0p68L0p68`` (emitter 0.68x0.68um, AE=0.4624um^2)
  and ``sky130_fd_pr__pnp_05v5_W3p40L3p40`` (3.40x3.40um, AE=11.56um^2), each a
  three-terminal ``c b e`` subcircuit. Confirmed in
  ``~/.volare/sky130A/libs.tech/combined/continuous/models_bjt.spice``
  (``.subckt  sky130_fd_pr__pnp_05v5_W0p68L0p68 c b e mult=1`` -- the vendor's
  own provenance comment on this line notes the substrate pin was removed for
  backwards compatibility). The writer selects the variant whose nominal
  emitter area is nearest the device's measured ``AE`` (there is no
  continuously-parameterized cell to pass a geometry to); for this vertical
  PNP the collector *is* the substrate, and the extraction deck already ties
  the collector to ``substrate_net``, so no separate substrate pin is emitted.

Geometry values on every new ``X`` card use the same per-family style the MOS
path uses -- bare micrometres for sky130 (whose ``w``/``l`` are
micrometre-convention under its own ``.option scale=1.0u``), an explicit
micrometre-unit-suffixed literal for gf180mcu (whose ``r_length``/
``c_length`` etc. are raw-metre convention with no ambient scale). See
:data:`_GEOMETRY_STYLE_BY_FAMILY` for the reproduction behind each.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import klayout.db as kdb

#: Decimal places L/W (in micrometres) are rounded to before formatting onto
#: an emitted `X` card -- mirrors `extract.py`'s `_PARAM_PRECISION_UM` (same
#: value, kept as a separate constant to avoid a cross-module import cycle:
#: `extract.py` imports this module, not the reverse).
_PARAM_PRECISION_UM = 6

#: (deck_name, pdk_variant_family) -> {"nfet": <subckt-name>, "pfet": <subckt-name>}
#: See the module docstring for how each entry's subcircuit name and
#: parameter convention were verified against a real fetched PDK install.
_MOS_MODEL_TABLE: dict[tuple[str, str], dict[str, str]] = {
    ("sky130", "sky130"): {
        "nfet": "sky130_fd_pr__nfet_01v8",
        "pfet": "sky130_fd_pr__pfet_01v8",
    },
    ("gf180mcu", "gf180mcu"): {
        "nfet": "nfet_03v3",
        "pfet": "pfet_03v3",
    },
    # sg13g2's thin-oxide ("-LV") core devices (issue #1231). Confirmed
    # against a real fetched IHP-Open-PDK v0.3.0 install
    # (`scripts/fetch-ihp-sg13g2.sh`): `.subckt sg13_lv_nmos d g s b` /
    # `.subckt sg13_lv_pmos d g s b` in
    # `ihp-sg13g2/libs.tech/ngspice/models/sg13g2_moslv_mod.lib`, each taking
    # `w`/`l` plus `as`/`ad`/`ps`/`pd` (all defaulting to 0) -- the same
    # `d g s b <model> W=... L=...` call shape, and the same
    # area/perimeter-parameter set, as gf180mcu's pair above. The PDK's own
    # LVS deck extracts the matching layout devices under exactly these
    # names (`mos_extraction.lvs`'s `extract_devices(mos4('sg13_lv_nmos'),
    # ...)`), which is what `decks/sg13g2.py`'s
    # `nfet_provenance`/`pfet_provenance` cite.
    ("sg13g2", "sg13g2"): {
        "nfet": "sg13_lv_nmos",
        "pfet": "sg13_lv_pmos",
    },
    # sg13cmos5l's thin-oxide ("-LV") core devices (issue #1400). cmos5l's
    # own `libs.tech/ngspice/models/sg13g2_moslv_mod.lib` is a literal
    # symlink into a sibling `ihp-sg13g2` checkout (see
    # `decks/sg13cmos5l.py`'s module docstring for the full symlink-chain
    # provenance) -- independently fetched at the exact commit
    # `ihp-sg13cmos5l/.github/ihp-sg13g2.ref` pins
    # (`d2cc0355f26235c777dfcc6867b390fa1e78083f`) and confirmed to declare
    # `.subckt sg13_lv_nmos d g s b` / `.subckt sg13_lv_pmos d g s b`,
    # byte-identical names/terminal order/parameter set to sg13g2's own pair
    # above. cmos5l's own `mos_extraction.lvs` (also a resolved symlink to
    # the same sibling) extracts the matching layout devices under exactly
    # these names, which is what `decks/sg13cmos5l.py`'s
    # `nfet_provenance`/`pfet_provenance` cite.
    ("sg13cmos5l", "sg13cmos5l"): {
        "nfet": "sg13_lv_nmos",
        "pfet": "sg13_lv_pmos",
    },
}

#: The KLayout device-property key `extract.py` sets (`kdb.Device.set_property`)
#: on a MOS device it extracted from inside a
#: `~klayout_tools.decks.ExtractionDeck.mos_flavours` marker (issue #1111),
#: keyed by that entry's own `MOSFlavour.flavour` string (e.g. `"06v0"`).
#: `create_model_binding_delegate`'s writer reads it back via
#: `kdb.Device.property` to select the matching `_MOS_MODEL_FLAVOURS` entry
#: below instead of the device class's base subcircuit -- the *only* consumer
#: of this property. It is deliberately not a new `devices[].class` label (see
#: `MOSFlavour`'s own docstring for why): every flavoured transistor still
#: extracts under the deck's ordinary `nfet_class`/`pfet_class`, so this
#: property is invisible to `device_counts`, `klt lvs` device-class matching,
#: and the unbound `M`-card writer (which never reads device properties).
MOS_FLAVOUR_PROPERTY = "mos_flavour"

#: (deck_name, pdk_variant_family) -> {flavour -> {"nfet": <subckt-name>,
#: "pfet": <subckt-name>}}. The additive, per-flavour sibling of
#: `_MOS_MODEL_TABLE` above (issue #1111, option 2 of #552): `flavour` is the
#: same string an `ExtractionDeck.mos_flavours[].flavour` entry declares
#: (e.g. gf180mcu's `"06v0"`, `decks/gf180mcu.py`'s `Dualgate`-scoped entry).
#: `resolve_device_bindings` folds this into each MOS `DeviceBinding`'s
#: `flavour_subckts` map; `known_mos_subckt_names`/`build_subckt_to_class_map`
#: fold it into the *base* `"nfet"`/`"pfet"` role instead (deliberately -- see
#: those functions' own docstrings) so a flavour subcircuit name resolves back
#: to the same structural device class its base sibling does.
#:
#: `nfet_06v0`/`pfet_06v0` confirmed in the same real fetched gf180mcuA
#: install the module docstring's MOS section cites for `nfet_03v3`
#: (`~/.volare/gf180mcuA/libs.tech/ngspice/sm141064.ngspice`'s `.subckt
#: nfet_06v0 d g s b w=... l=... + as=0 ad=0 ps=0 pd=0 ...`, same `d g s b`
#: terminal order and `as`/`ad`/`ps`/`pd` call-site parameters as the 3.3V
#: pair) and a real in-the-wild instantiation
#: (`~/.volare/gf180mcuA/libs.ref/gf180mcu_fd_io/spice/gf180mcu_fd_io.spice`'s
#: `X5 n56 IE VSS VSS nfet_06v0 m=1.0 w=1.5e-6 l=700e-9 ...`, already cited by
#: the module docstring's MOS section). `pfet_06v0` follows the identical
#: no-prefix, `d g s b`-terminal, raw-metre-`w`/`l` convention (not
#: independently re-verified in a second in-the-wild instantiation, but from
#: the same `.subckt`-table source as `nfet_06v0`).
#:
#: sg13g2's thick-oxide ("-HV") pair (issue #1231) is confirmed in the same
#: real fetched IHP-Open-PDK v0.3.0 install as its thin-oxide siblings above:
#: `.subckt sg13_hv_nmos d g s b` / `.subckt sg13_hv_pmos d g s b` in
#: `ihp-sg13g2/libs.tech/ngspice/models/sg13g2_moshv_mod.lib` (same terminal
#: order and `w`/`l`/`as`/`ad`/`ps`/`pd` parameter set as the `_lv_` pair),
#: extracted under exactly those names by the PDK's own
#: `mos_extraction.lvs` (`extract_devices(mos4('sg13_hv_nmos'), ...)` keyed
#: off `general_derivations.lvs`'s `ngate_hv_base = ngate.and(thickgateox_drw)`)
#: -- the derivation `decks/sg13g2.py`'s `mos_flavours` entry transcribes.
#:
#: sky130's 5V-gate/10.5V-drain thick-oxide pair (issue #1369) is confirmed in
#: the same real fetched sky130A install (volare,
#: `c6d73a35f524070e85faff4a6a9eef49553ebc2b`) the module docstring's `01v8`
#: entry above cites -- `libs.tech/combined/continuous/models_fet.spice:15382`
#: (`.subckt  sky130_fd_pr__nfet_g5v0d10v5  d g s b  mult=1`) and `:36917`
#: (the `pfet` analogue), each followed by the identical
#: `.param  l = 1 w = 1 nf = 1 ad = 0 as = 0 pd = 0 ps = 0 ...` line the
#: `01v8` pair carries at `:10037`, i.e. the same `d g s b` terminal order
#: and `l`/`w`/`as`/`ad`/`ps`/`pd` call-site convention
#: :func:`create_model_binding_delegate` already writes for the default pair.
#: Extracted under exactly those names by the PDK's own
#: `libs.tech/klayout/lvs/sky130.lvs:2085`
#: (`extract_devices(mos4("sky130_fd_pr__nfet_g5v0d10v5"), { "SD" => nsd,
#: "G" => ngate_5p0v_hv, ... })`; the PMOS analogue at `:2059`, on
#: `pgate_5p0v_hv`), whose gate regions are keyed off `hvi` at `:941`
#: (`hvi         = polygons(75 , 20 )`) via `:1213`
#: (`ngate_high_voltage = ngate.and(hvi)...`, feeding `ngate_5p0v_hv` at
#: `:1229`) and `:1190`/`:1202` for the PMOS side -- the derivation
#: `decks/sky130.py`'s `mos_flavours` entry transcribes (as the same
#: any-overlap-counts approximation every other `mos_flavours` entry in this
#: table uses, see `MOSFlavour`'s own docstring in `decks/__init__.py`). The
#: `hvi` layer/datatype pair itself is cross-checked against two further
#: independent sources in the same install: the KLayout layer-properties
#: file `libs.tech/klayout/tech/sky130A.lyp:3127`
#: (`<name>hvi.drawing - 75/20</name>`) and the magic technology file
#: `libs.tech/magic/sky130A.tech:3980` (`calma HVI 75 20`).
_MOS_MODEL_FLAVOURS: dict[tuple[str, str], dict[str, dict[str, str]]] = {
    ("gf180mcu", "gf180mcu"): {
        "06v0": {
            "nfet": "nfet_06v0",
            "pfet": "pfet_06v0",
        },
    },
    ("sg13g2", "sg13g2"): {
        "hv": {
            "nfet": "sg13_hv_nmos",
            "pfet": "sg13_hv_pmos",
        },
    },
    # sg13cmos5l's thick-oxide ("-HV") pair (issue #1416, the sg13cmos5l
    # sibling of sg13g2's own #1231 entry above). cmos5l's own
    # `libs.tech/ngspice/models/sg13g2_moshv_mod.lib` is a literal symlink
    # into a sibling `ihp-sg13g2` checkout (see `decks/sg13cmos5l.py`'s
    # module docstring for the full symlink-chain provenance) --
    # independently fetched at the exact commit
    # `ihp-sg13cmos5l/.github/ihp-sg13g2.ref` pins
    # (`d2cc0355f26235c777dfcc6867b390fa1e78083f`) and confirmed to declare
    # `.subckt sg13_hv_nmos d g s b` / `.subckt sg13_hv_pmos d g s b`,
    # byte-identical names/terminal order/parameter set to sg13g2's own HV
    # pair above -- not assumed identical by analogy (per
    # `docs/guides/pdk-family-port-checklist.md` step 2). cmos5l's own
    # `mos_extraction.lvs` (also a resolved symlink to the same sibling)
    # extracts the matching layout devices under exactly these names, which
    # is what `decks/sg13cmos5l.py`'s `mos_flavours[0].nfet_provenance`/
    # `.pfet_provenance` cite.
    ("sg13cmos5l", "sg13cmos5l"): {
        "hv": {
            "nfet": "sg13_hv_nmos",
            "pfet": "sg13_hv_pmos",
        },
    },
    ("sky130", "sky130"): {
        "hvi": {
            "nfet": "sky130_fd_pr__nfet_g5v0d10v5",
            "pfet": "sky130_fd_pr__pfet_g5v0d10v5",
        },
    },
}

#: PDK families this table knows how to classify a resolved `--pdk` variant
#: name into (e.g. "sky130A"/"sky130B" -> "sky130", "gf180mcuA".."D" ->
#: "gf180mcu") -- the same family prefixes `klayout_tools.decks`' registry
#: uses as deck names. Order matters only in that no family here is a prefix
#: of another (`"sg13g2"` and `"sg13cmos5l"` share the `"sg13"` stem but
#: neither is a prefix of the other, so both are safe to list).
_KNOWN_PDK_FAMILIES: tuple[str, ...] = ("sky130", "gf180mcu", "sg13g2", "sg13cmos5l")

#: Resolved `--pdk` variant names whose family is *not* a prefix of the
#: variant name, and so cannot be recovered by the prefix scan below (issue
#: #1231). IHP-Open-PDK's SG13G2 install directory -- the variant name
#: `klt pdk find` reports -- is `ihp-sg13g2`, while this repo's deck/family
#: name for it is `sg13g2` (`klayout_tools.decks.sg13g2`); an explicit alias
#: keeps families named after decks, the invariant `_KNOWN_PDK_FAMILIES`'
#: own comment states, rather than introducing a family spelled differently
#: from every table key around it. `ihp-sg13cmos5l` (issue #1400) is the
#: same shape: the standalone `ihp-sg13cmos5l` clone's own directory name is
#: the resolved variant, while this repo's deck/family name is
#: `sg13cmos5l` (`klayout_tools.decks.sg13cmos5l`).
_PDK_VARIANT_FAMILY_ALIASES: dict[str, str] = {
    "ihp-sg13g2": "sg13g2",
    "ihp-sg13cmos5l": "sg13cmos5l",
}

#: Geometry-literal style: write an explicit SPICE unit suffix (``L=0.5U``,
#: ``AS=0.84P``), i.e. an absolute SI value that does not depend on the
#: caller's ``.option scale``.
GEOMETRY_STYLE_UNIT_SUFFIX = "unit_suffix"

#: Geometry-literal style: write a bare, suffix-free number already expressed
#: in micrometres (``L=0.5``) or square micrometres (``AS=0.84``), which the
#: deck's own ambient ``.option scale=1.0u`` converts to SI.
GEOMETRY_STYLE_BARE_UM = "bare_um"

#: PDK family -> the geometry-literal style :func:`create_model_binding_delegate`
#: writes ``L``/``W``/``AS``/``AD``/``PS``/``PD`` in for that family's bound
#: ``X`` cards (issue #1396). Families absent here keep
#: :data:`GEOMETRY_STYLE_UNIT_SUFFIX`, the module's original behaviour.
#:
#: This table exists because the module's original premise -- "an explicit SI
#: unit suffix is parsed identically regardless of any ``.option scale``, so
#: it is correct under every PDK without special-casing" -- is **false** for a
#: vendor deck that sets ``.option scale`` itself. ngspice's ``scale`` option
#: multiplies a MOS card's ``l``/``w``/``ps``/``pd`` by ``scale`` and its
#: ``as``/``ad`` by ``scale^2`` *after* parsing the literal, so under a
#: ``scale=1.0u`` deck a unit-suffixed ``W=2U`` becomes ``2e-12`` m, not
#: ``2e-6`` m.
#:
#: - ``sky130`` -> :data:`GEOMETRY_STYLE_BARE_UM`. Verified against a real
#:   fetched sky130A install (open_pdks): ``libs.tech/combined/corners/all.spice``
#:   line 29 is ``.option scale=1.0u``, and the vendor's own comment there
#:   states the convention outright -- *"The scale option forces all netlists
#:   to provide distance units in microns (e.g., 1 micron width is W=1, not
#:   W=1u)."* Every sky130 primitive subcircuit forwards its call-site
#:   geometry straight onto an internal scaled card (e.g.
#:   ``.subckt sky130_fd_pr__nfet_01v8 d g s b`` -> ``msky130_fd_pr__nfet_01v8
#:   ... l={l} w={w} ...``), so a unit-suffixed literal is off by ``1e6``
#:   there: reproduced with ngspice 46, where ``L=0.5U W=2U AS=0.84P AD=0.84P
#:   PS=4.84U PD=4.84U`` misses every model bin (``could not find a valid
#:   modelname``, with the model's internally defaulted ``nrd``/``nrs``
#:   reported as ``7e+04``) while the identical geometry as ``L=0.5 W=2
#:   AS=0.84 AD=0.84 PS=4.84 PD=4.84`` solves to a sane operating point. The
#:   same 1e6 error hits this family's resistor and capacitor bindings, whose
#:   subcircuits are geometry-parameterized under the same ambient scale
#:   (``sky130_fd_pr__res_xhigh_po l=5U w=2U`` fails the deck's own parse-tree
#:   check; ``sky130_fd_pr__cap_mim_m3_1 w=10U l=10U`` silently models a
#:   ~4600x-too-small capacitor), which is why the style is keyed by family
#:   rather than by device kind.
#: - ``gf180mcu`` -> :data:`GEOMETRY_STYLE_UNIT_SUFFIX` (unchanged). Its model
#:   library sets no ``.option scale`` at all (verified: no ``.option`` line in
#:   ``libs.tech/ngspice/sm141064.ngspice``) and its subcircuits declare
#:   raw-metre defaults (``.subckt nfet_03v3 d g s b w=1e-5 l=2.8e-7 ...``), so
#:   the absolute, suffix-carrying literal is the correct form for this family
#:   and switching it to bare micrometres would break it by the same 1e6.
#: - ``sg13g2`` -> absent, so unchanged at the unit-suffix default. Confirmed
#:   correct (not merely left unverified) as of issue #1457: no ``.option
#:   scale`` line anywhere under a real fetched IHP-Open-PDK v0.3.0 install's
#:   ``libs.tech/ngspice/``, and every subcircuit this module binds --
#:   ``sg13_lv_nmos``/``sg13_lv_pmos`` (``.subckt sg13_lv_nmos d g s b w=0.35u
#:   l=0.34u ...``) and ``rsil``/``rppd``/``rhigh`` (``.subckt rsil 1 2 bn``,
#:   ``.param w=0.5e-6 l=0.5e-6 ...``) -- declares raw-metre defaults, the same
#:   convention gf180mcu uses.
_GEOMETRY_STYLE_BY_FAMILY: dict[str, str] = {
    "sky130": GEOMETRY_STYLE_BARE_UM,
    "gf180mcu": GEOMETRY_STYLE_UNIT_SUFFIX,
}


def geometry_style_for_family(family: str) -> str:
    """The geometry-literal style bound ``X`` cards use for PDK ``family``
    (issue #1396) -- :data:`GEOMETRY_STYLE_UNIT_SUFFIX` for any family with no
    curated entry in :data:`_GEOMETRY_STYLE_BY_FAMILY`, i.e. this module's
    original behaviour."""
    return _GEOMETRY_STYLE_BY_FAMILY.get(family, GEOMETRY_STYLE_UNIT_SUFFIX)


class ModelBindingError(Exception):
    """Raised when a resolved PDK variant has no curated MOS device-model
    table entry for the extraction deck in use.

    Covers both an unrecognised PDK family (a variant name not matching any
    known family prefix) and a recognised family with no curated entry for
    the requesting deck (e.g. running the ``sky130`` deck against a resolved
    ``gf180mcuA`` install) -- both are a deck/PDK mismatch the caller should
    see as a named, actionable error, never a silent fallback to the bare
    ``M``-card form (see ``docs/cli/extract.md``).
    """


def _pdk_variant_family(variant: str) -> str:
    """The PDK-family portion of a resolved ``--pdk`` variant name (e.g.
    ``"sky130A"`` -> ``"sky130"``, ``"gf180mcuC"`` -> ``"gf180mcu"``,
    ``"ihp-sg13g2"`` -> ``"sg13g2"``).

    Returns ``variant`` unchanged when it does not match any known family
    prefix -- :func:`resolve_mos_model_table` turns that into a named
    :class:`ModelBindingError` rather than this helper raising, so the
    error message can report the full attempted ``(deck, variant)`` pair.
    """
    aliased = _PDK_VARIANT_FAMILY_ALIASES.get(variant)
    if aliased is not None:
        return aliased
    for family in _KNOWN_PDK_FAMILIES:
        if variant.startswith(family):
            return family
    return variant


def resolve_mos_model_table(deck_name: str, pdk_variant: str) -> dict[str, str]:
    """Resolve the curated ``{"nfet": ..., "pfet": ...}`` subcircuit-name
    table for ``deck_name`` against ``pdk_variant``'s PDK family.

    Raises :class:`ModelBindingError` (which ``extract.py`` turns into an
    :class:`~klayout_tools.extract.ExtractError`) when ``pdk_variant``'s
    family is unrecognised, or is recognised but has no curated entry for
    ``deck_name`` -- never returns a partial/guessed table.
    """
    family = _pdk_variant_family(pdk_variant)
    table = _MOS_MODEL_TABLE.get((deck_name, family))
    if table is None:
        available = ", ".join(
            f"deck '{d}' + PDK family '{f}'" for d, f in sorted(_MOS_MODEL_TABLE)
        )
        raise ModelBindingError(
            f"no curated PDK device-model binding for deck '{deck_name}' + "
            f"PDK variant '{pdk_variant}' (resolved family '{family}'); "
            f"available bindings: {available}"
        )
    return table


def resolve_mos_model_table_for_deck(deck_name: str) -> dict[str, str]:
    """Resolve the curated ``{"nfet": ..., "pfet": ...}`` subcircuit-name
    table for ``deck_name`` when only the deck name is known (no resolved
    ``--pdk`` variant), as in a ``klt lvs`` request that names a curated deck
    but resolves no PDK install.

    The curated table's deck names coincide with its PDK-family keys
    (``sky130``/``gf180mcu`` are both), so this is
    :func:`resolve_mos_model_table` with the deck name standing in for the
    variant -- it raises the same :class:`ModelBindingError` for an unknown
    deck rather than returning a partial/guessed table.
    """
    return resolve_mos_model_table(deck_name, deck_name)


def build_subckt_to_class_map(deck_name: str) -> dict[str, str]:
    """Reverse of :func:`resolve_mos_model_table_for_deck`: a
    ``<subckt-name> -> <device-class>`` map (e.g.
    ``"sky130_fd_pr__nfet_01v8" -> "nfet"``) for turning a PDK schematic
    flow's subcircuit-call device cards *back* into the curated deck's
    plain-element device form (the ``klt lvs`` reference-netlist direction,
    issue #280) -- the mirror of the writer delegate's plain-element ->
    subckt-call direction (:func:`create_model_binding_delegate`).

    Every ``_MOS_MODEL_FLAVOURS`` subcircuit name (issue #1111, e.g.
    gf180mcu's ``nfet_06v0``/``pfet_06v0``) resolves to the *same* base
    ``"nfet"``/``"pfet"`` device class its default-flavour sibling does, not
    a separate class of its own: a reference netlist that instantiates a
    flavour subcircuit directly is still structurally an ordinary MOS device
    for ``klt lvs`` comparison purposes (the extracted layout side reports
    every MOS device -- flavoured or not -- under the same base class too,
    see ``MOSFlavour``'s own docstring in ``decks/__init__.py`` for why), so
    this reverse direction deliberately does not distinguish them.

    Raises :class:`ModelBindingError` (via
    :func:`resolve_mos_model_table_for_deck`) for an unknown deck.
    """
    result = {
        subckt: device_class
        for device_class, subckt in resolve_mos_model_table_for_deck(deck_name).items()
    }
    for flavour_table in _MOS_MODEL_FLAVOURS.get((deck_name, deck_name), {}).values():
        for device_class, subckt in flavour_table.items():
            result.setdefault(subckt, device_class)
    return result


def known_mos_subckt_names() -> dict[str, tuple[str, str]]:
    """Every curated MOS device subcircuit name across *all* decks, mapped to
    the ``(deck_name, device_class)`` it resolves to (e.g.
    ``"nfet_03v3" -> ("gf180mcu", "nfet")``).

    Used for *detection* without a caller-supplied deck: a ``klt lvs``
    reference netlist that instantiates one of these names via an ``X``
    subcircuit call is in the simulation (subckt-call) form, not the
    schematic-equivalent plain-element form ``klt lvs`` requires (issue #280).
    Subcircuit names are unique across the curated decks, so the mapping is
    unambiguous.

    Includes every ``_MOS_MODEL_FLAVOURS`` entry (issue #1111, e.g.
    gf180mcu's ``nfet_06v0``/``pfet_06v0``), resolved to the same
    ``(deck_name, "nfet"|"pfet")`` base device class its default-flavour
    sibling is -- see :func:`build_subckt_to_class_map`'s docstring for why.
    """
    result: dict[str, tuple[str, str]] = {}
    for (deck_name, _family), table in _MOS_MODEL_TABLE.items():
        for device_class, subckt in table.items():
            result[subckt] = (deck_name, device_class)
    for (deck_name, _family), flavours in _MOS_MODEL_FLAVOURS.items():
        for flavour_table in flavours.values():
            for device_class, subckt in flavour_table.items():
                result.setdefault(subckt, (deck_name, device_class))
    return result


@dataclass(frozen=True)
class DeviceLookup:
    """One entry of the netlist-*ingestion*-direction reverse lookup table
    (issue #1130): the sibling of :class:`DeviceBinding` for turning a
    subcircuit-call ``X`` card *back* into a plain-element card
    (:mod:`klayout_tools.netlist_normalize`'s job), built entirely from this
    module's static curated subcircuit-name tables -- unlike
    :func:`resolve_device_bindings`, this direction never needs a live
    ``ExtractionDeck`` object (a deck's own ``resistors``/``capacitors``/
    ``bipolars`` lists), only the fixed ``<device-class> -> <subckt-name>``
    tables that are already deck-name-keyed.

    ``kind`` mirrors :attr:`DeviceBinding.kind`
    (``"mos"``/``"resistor"``/``"capacitor"``/``"bipolar"``). ``device_class``
    is the plain-element card's model-field label (``nfet``,
    ``res_generic_po``, ...). ``length_param``/``width_param`` are the *real*
    subcircuit's own call-site parameter spellings (``l``/``w`` for sky130,
    ``r_length``/``r_width`` or ``c_length``/``c_width`` for gf180mcu) --
    ``None`` for a kind with no geometry call-site parameter (bipolar's
    fixed-geometry cells, see :func:`known_device_subckt_names`'s
    docstring).
    """

    kind: str
    device_class: str
    length_param: str | None = None
    width_param: str | None = None


def known_device_subckt_names() -> dict[str, tuple[str, DeviceLookup]]:
    """Every curated device subcircuit name across *all* decks and *all*
    device families (MOS, resistor, capacitor, bipolar), mapped to the
    ``(deck_name, DeviceLookup)`` it resolves to (issue #1130's
    device-family extension of :func:`known_mos_subckt_names`, used the same
    way: auto-resolution when a caller gives no explicit ``deck``).

    A curated bipolar subcircuit (e.g. sky130's fixed-geometry
    ``sky130_fd_pr__pnp_05v5_W0p68L0p68``) carries no call-site
    length/width-style parameter at all -- unlike MOS/resistor/capacitor, an
    ``X`` card instantiating one cannot be recognised by a carried-parameter
    heuristic, only by this table's subcircuit name itself
    (:mod:`klayout_tools.netlist_normalize` resolves bipolar purely by name
    for exactly this reason).
    """
    result: dict[str, tuple[str, DeviceLookup]] = {}
    for subckt, (deck_name, device_class) in known_mos_subckt_names().items():
        result[subckt] = (deck_name, DeviceLookup("mos", device_class, "l", "w"))
    for (deck_name, family), table in _RESISTOR_MODEL_TABLE.items():
        length_param, width_param = _RESISTOR_PARAM_STYLE.get(family, ("l", "w"))
        for device_class, subckt in table.items():
            result.setdefault(
                subckt,
                (
                    deck_name,
                    DeviceLookup("resistor", device_class, length_param, width_param),
                ),
            )
    for (deck_name, family), table in _CAPACITOR_MODEL_TABLE.items():
        length_param, width_param = _CAPACITOR_PARAM_STYLE.get(family, ("l", "w"))
        for device_class, subckt in table.items():
            result.setdefault(
                subckt,
                (
                    deck_name,
                    DeviceLookup("capacitor", device_class, length_param, width_param),
                ),
            )
    for (deck_name, _family), table in _BIPOLAR_MODEL_TABLE.items():
        for device_class, variants in table.items():
            for _nominal_ae, subckt in variants:
                result.setdefault(
                    subckt, (deck_name, DeviceLookup("bipolar", device_class))
                )
    return result


def _declared_non_mos_classes(
    deck_name: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(resistor class names, capacitor class names)`` the registered
    :class:`~klayout_tools.decks.ExtractionDeck` for ``deck_name`` declares --
    the deck-derived input to :func:`build_device_binding_map`'s
    assumed-identity fallback (issue #1464).

    Imported lazily, inside the function, on purpose: this module's whole
    point is that the *ingestion* direction needs only deck-name-keyed static
    tables (see :class:`DeviceLookup`), so a module-level ``decks`` import
    would make every ``pdk_models`` consumer pay for the deck registry. An
    unknown/unregistered deck yields ``((), ())`` rather than raising, so the
    fallback can only ever *add* bindings -- it never turns a deck that
    resolved before into one that fails.
    """
    from .decks import UnknownExtractionDeckError, get_extraction_deck

    try:
        deck = get_extraction_deck(deck_name)
    except UnknownExtractionDeckError:
        return (), ()
    return (
        tuple(resistor.name for resistor in deck.resistors),
        tuple(capacitor.name for capacitor in deck.capacitors),
    )


def build_device_binding_map(deck_name: str) -> dict[str, DeviceLookup]:
    """Reverse of :func:`resolve_device_bindings`'s static (deck-object-free)
    portion: ``<subckt-name> -> DeviceLookup`` for every curated MOS,
    resistor, capacitor, and bipolar device this table knows for
    ``deck_name`` (issue #1130's device-family extension of
    :func:`build_subckt_to_class_map`).

    **Assumed-identity fallback (issue #1464)**: a deck whose resistor (or
    capacitor) family has *no* curated ``(deck_name, family)`` entry in
    :data:`_RESISTOR_MODEL_TABLE` (:data:`_CAPACITOR_MODEL_TABLE`) at all
    additionally gets one binding per class its own
    ``ExtractionDeck.resistors``/``.capacitors`` declares, taking the
    declared LVS device-class name as its subcircuit name. Without it, a deck
    that recognises a device for *extraction* could not read that same device
    back on ``klt lvs``'s reference side -- the round-trip asymmetry #1464
    reported for ``sg13cmos5l``'s ``rsil``/``rppd``/``rhigh`` (declared since
    #1415) and, independently, for ``sg13g2``'s ``cap_cmim``/``rfcmim``
    (declared since #1456). Because it is derived, a *future* class a deck
    declares is covered with no edit to this module at all.

    Three deliberate limits on that fallback, each protecting a verified
    curated fact from being overwritten by a guess:

    - It is gated on the whole ``(deck_name, family)`` pair being absent, not
      merely on the individual class being absent. A pair that *does* have a
      curated entry was hand-verified against a real fetched PDK install --
      including which declared classes deliberately have **no** subcircuit to
      bind to (``sg13g2``'s ``res_metal1``/``res_metal2``, see
      :data:`_RESISTOR_MODEL_TABLE`). Deriving identity for those would
      fabricate exactly the bindings that verification ruled out.
    - Every insertion is a :meth:`dict.setdefault`, so a curated entry always
      wins -- notably sky130's genuinely non-identity
      ``sky130_fd_pr__model__cap_mim -> sky130_fd_pr__cap_mim_m3_1``, which
      the pair gate already keeps out of the fallback's reach.
    - **Bipolar is excluded.** The only deck with a declared-but-unbound
      bipolar is gf180mcu, whose absence from :data:`_BIPOLAR_MODEL_TABLE` is
      a *verified* "no positively-identified device cell exists" carve-out
      (``decks/gf180mcu.py``, ``docs/cli/extract.md`` -> "Scope limits"), not
      an unvisited pair. A bipolar ``X`` card also carries no geometry
      parameter to cross-check the name against (see
      :func:`known_device_subckt_names`), so a fabricated ``bjt`` binding
      would silently rewrite a genuine hierarchical ``X ... bjt`` instance
      into a ``Q`` card.

    The fallback is confined to this ingestion-direction function: it does
    **not** feed :func:`resolve_device_bindings`, so ``klt extract --pdk``'s
    bare-primitive-card carve-outs are untouched.

    Raises :class:`ModelBindingError` (via :func:`build_subckt_to_class_map`)
    for an unknown deck -- never returns a partial/guessed table.
    """
    result: dict[str, DeviceLookup] = {
        subckt: DeviceLookup("mos", device_class, "l", "w")
        for subckt, device_class in build_subckt_to_class_map(deck_name).items()
    }
    family = deck_name
    res_len, res_wid = _RESISTOR_PARAM_STYLE.get(family, ("l", "w"))
    res_table = _RESISTOR_MODEL_TABLE.get((deck_name, family))
    for device_class, subckt in (res_table or {}).items():
        result.setdefault(
            subckt, DeviceLookup("resistor", device_class, res_len, res_wid)
        )
    cap_len, cap_wid = _CAPACITOR_PARAM_STYLE.get(family, ("l", "w"))
    cap_table = _CAPACITOR_MODEL_TABLE.get((deck_name, family))
    for device_class, subckt in (cap_table or {}).items():
        result.setdefault(
            subckt, DeviceLookup("capacitor", device_class, cap_len, cap_wid)
        )
    for device_class, variants in _BIPOLAR_MODEL_TABLE.get(
        (deck_name, family), {}
    ).items():
        for _nominal_ae, subckt in variants:
            result.setdefault(subckt, DeviceLookup("bipolar", device_class))
    if res_table is None or cap_table is None:
        declared_resistors, declared_capacitors = _declared_non_mos_classes(deck_name)
        if res_table is None:
            for device_class in declared_resistors:
                result.setdefault(
                    device_class,
                    DeviceLookup("resistor", device_class, res_len, res_wid),
                )
        if cap_table is None:
            for device_class in declared_capacitors:
                result.setdefault(
                    device_class,
                    DeviceLookup("capacitor", device_class, cap_len, cap_wid),
                )
    return result


def _round_um_text(value: float) -> str:
    """``value`` rounded to :data:`_PARAM_PRECISION_UM` decimal places and
    rendered without unnecessary trailing zeros (``8.0`` -> ``"8"``, ``0.5``
    -> ``"0.5"``, ``0.0`` -> ``"0"``) -- the shared numeric half of
    :func:`_format_um`/:func:`_format_um2`, before either applies (or omits) a
    unit suffix."""
    rounded = round(value, _PARAM_PRECISION_UM)
    text = f"{rounded:.{_PARAM_PRECISION_UM}f}".rstrip("0").rstrip(".")
    return text or "0"


def _format_um(value: float, style: str = GEOMETRY_STYLE_UNIT_SUFFIX) -> str:
    """Format a micrometre value for a device card's length-dimensioned
    parameter (``L``/``W``/``PS``/``PD``).

    Under the default :data:`GEOMETRY_STYLE_UNIT_SUFFIX` this matches KLayout's
    own default ``M``-card writer exactly (``8.0`` -> ``"8U"``, ``0.5`` ->
    ``"0.5U"``) -- an explicit unit suffix, no unnecessary trailing zeros.
    Under :data:`GEOMETRY_STYLE_BARE_UM` the same number is written with no
    suffix (``"8"``, ``"0.5"``), because the target deck's own ambient
    ``.option scale=1.0u`` supplies the micrometre unit and would otherwise
    scale a suffixed literal a second time (issue #1396 -- see
    :data:`_GEOMETRY_STYLE_BY_FAMILY`).
    """
    text = _round_um_text(value)
    return text if style == GEOMETRY_STYLE_BARE_UM else f"{text}U"


def _format_um2(value: float, style: str = GEOMETRY_STYLE_UNIT_SUFFIX) -> str:
    """Format a square-micrometre value for a device card's area-dimensioned
    parameter (``AS``/``AD``, and ``A`` on a normalized ``C`` card).

    Under the default :data:`GEOMETRY_STYLE_UNIT_SUFFIX` this matches KLayout's
    own default ``M``-card writer (``0.8`` -> ``"0.8P"``) -- ``1 um^2 ==
    1e-12 m^2``, the same order of magnitude the SPICE ``P`` (pico, ``1e-12``)
    unit suffix denotes, so a value already expressed in square micrometres
    needs no numeric conversion at all, only swapping :func:`_format_um`'s
    ``"U"`` (micro) suffix for ``"P"`` (issue #695). Under
    :data:`GEOMETRY_STYLE_BARE_UM` the same number is written with no suffix
    (``"0.8"``): ngspice scales a MOS card's ``as``/``ad`` by ``scale^2``, so a
    bare square-micrometre number is exactly what a ``scale=1.0u`` deck wants
    (issue #1396).
    """
    text = _round_um_text(value)
    return text if style == GEOMETRY_STYLE_BARE_UM else f"{text}P"


def equivalent_rectangle_um(
    area_um2: float, perimeter_um: float
) -> tuple[float, float] | None:
    """The ``(length, width)`` of the single rectangle with the given ``area``
    and ``perimeter`` (``length >= width``), or ``None`` when no real rectangle
    has both (the shapes are "rounder" than any rectangle -- a negative
    discriminant, e.g. a single square-ish pad seen through a fragmented
    outline, or non-positive inputs).

    The side lengths are the roots of ``t^2 - (P/2) t + A = 0``; this is the
    same first-order equivalent-rectangle model ``extract.py``'s
    :func:`~klayout_tools.extract._n_squares` uses to estimate a net's square
    count (that helper delegates here, then takes ``length / width``), factored
    out so the ``--pdk`` capacitor-binding path can recover a geometry-
    parameterized MiM subcircuit's ``l``/``w`` from the extracted plate
    area+perimeter without duplicating the quadratic (issue #339).
    """
    if area_um2 <= 0.0 or perimeter_um <= 0.0:
        return None
    half_p = perimeter_um / 2.0
    disc = half_p * half_p - 4.0 * area_um2
    if disc < 0.0:
        return None
    length = (half_p + math.sqrt(disc)) / 2.0
    width = area_um2 / length
    if width <= 0.0:
        return None
    return (length, width)


@dataclass(frozen=True)
class DeviceBinding:
    """How :func:`create_model_binding_delegate` rewrites one extracted device
    class onto a real PDK subcircuit call (issue #339).

    ``kind`` selects the writer strategy:

    - ``"mos"`` -- four terminals ``D G S B``, ``L``/``W`` read straight off
      the device (the original #209 behavior, now expressed through this
      shared structure so its output stays byte-identical).
    - ``"resistor"`` -- terminals ``A B`` (plus ``W`` bulk tie for a
      ``DeviceExtractorResistorWithBulk`` device), ``L``/``W`` read off the
      device (KLayout's resistor extractor natively carries them).
    - ``"capacitor"`` -- terminals ``A B``, plate ``L``/``W`` derived from the
      device's ``A``/``P`` via :func:`equivalent_rectangle_um` (the extractor
      exposes area/perimeter, not ``L``/``W``).
    - ``"bipolar"`` -- terminals ``C B E`` (matching the vendor subcircuit's
      real three-terminal ``c b e`` declaration); no ``L``/``W`` params (the
      geometry is encoded by :attr:`variants`, selected by measured ``AE``).

    ``length_param``/``width_param`` are the subcircuit's own spellings of its
    length/width parameters (``l``/``w`` for sky130, ``r_length``/``r_width``
    or ``c_length``/``c_width`` for gf180mcu -- see the module docstring's
    verified-provenance section). ``variants`` is the ``(nominal AE in um^2,
    subcircuit name)`` selection table for a geometry-named bipolar family.

    ``dropped_params`` names every parameter the extractor measures on this
    device class that this binding deliberately does **not** carry onto the
    emitted ``X`` card, because the resolved target subcircuit has no
    parameter for it (issue #695's acceptance criterion: dropping a measured
    value silently is not acceptable -- naming it in ``warnings[]`` is). Empty
    for ``"mos"`` (its target subcircuits all declare ``as``/``ad``/``ps``/
    ``pd``, so nothing is dropped) and ``"resistor"``/``"capacitor"`` (their
    only extractor-measured geometry, ``L``/``W`` or the ``A``/``P`` it is
    derived from, is fully carried). Non-empty for ``"bipolar"``: sky130's
    ``pnp_05v5`` cells are fixed-geometry (selected by name, not
    parameterized -- see :attr:`variants`), so the extractor's measured base/
    collector area+perimeter and emitter count have no call-site parameter to
    land on at all.

    ``flavour_subckts`` (issue #1111, ``"mos"`` only) is an optional
    ``{MOSFlavour.flavour -> subcircuit name}`` override map. The writer
    checks it *before* falling back to :attr:`subckt`: when the device
    carries the ``pdk_models.MOS_FLAVOUR_PROPERTY`` KLayout property (set by
    ``extract.py`` for a transistor recognised inside one of the deck's
    ``mos_flavours`` markers) and the property's value is a key of this map,
    the mapped subcircuit is used instead of the class's default one -- e.g.
    gf180mcu's ``"nfet"`` binding's default ``subckt`` stays ``nfet_03v3``,
    but ``flavour_subckts={"06v0": "nfet_06v0"}`` rebinds a Dualgate-scoped
    device to ``nfet_06v0`` without needing a distinct KLayout device class
    (see ``MOSFlavour``'s own docstring in ``decks/__init__.py`` for why a
    distinct class was deliberately avoided). Empty for every device class
    with no declared flavour -- every class before this field existed, and
    every non-``"mos"`` kind today.

    ``geometry_style`` (issue #1396) is the resolved PDK family's
    geometry-literal convention -- :data:`GEOMETRY_STYLE_UNIT_SUFFIX` (write
    ``L=0.5U``/``AS=0.84P``) or :data:`GEOMETRY_STYLE_BARE_UM` (write
    ``L=0.5``/``AS=0.84``, letting the vendor deck's own ``.option
    scale=1.0u`` supply the unit). It is a property of the *target deck*, not
    of the device kind, so :func:`resolve_device_bindings` stamps the same
    value onto every binding it builds for one family; see
    :data:`_GEOMETRY_STYLE_BY_FAMILY` for the per-family evidence. Defaults to
    the unit-suffix form, which is what every caller got before this field
    existed.
    """

    kind: str
    subckt: str
    terminals: tuple[str, ...]
    length_param: str = "L"
    width_param: str = "W"
    variants: tuple[tuple[float, str], ...] = field(default_factory=tuple)
    dropped_params: tuple[str, ...] = field(default_factory=tuple)
    flavour_subckts: dict[str, str] = field(default_factory=dict)
    geometry_style: str = GEOMETRY_STYLE_UNIT_SUFFIX


#: (deck_name, pdk_variant_family) -> {deck ResistorDevice.name -> subckt name}.
#: See the module docstring's verified-provenance section for the real fetched
#: install each subcircuit name and parameter convention was read from.
_RESISTOR_MODEL_TABLE: dict[tuple[str, str], dict[str, str]] = {
    ("sky130", "sky130"): {
        "res_generic_po": "sky130_fd_pr__res_generic_po",
        "res_high_po": "sky130_fd_pr__res_high_po",
        "res_xhigh_po": "sky130_fd_pr__res_xhigh_po",
    },
    ("gf180mcu", "gf180mcu"): {
        "ppolyf_u": "ppolyf_u",
        # All three high-sheet-rho flavours (issue #595): which one a given
        # extraction reports is chosen by `--deck-option poly_res={1k,2k,3k}`,
        # so every selectable flavour needs its own binding entry or the
        # selected device class silently falls through to a bare `R` card.
        "ppolyf_u_1k": "ppolyf_u_1k",
        "ppolyf_u_2k": "ppolyf_u_2k",
        "ppolyf_u_3k": "ppolyf_u_3k",
    },
    # sg13g2's three drawn poly resistors (issue #1457; classes recognised by
    # issues #1231/#1235). Confirmed against a real fetched IHP-Open-PDK
    # v0.3.0 install (`scripts/fetch-ihp-sg13g2.sh`):
    # `libs.tech/ngspice/models/resistors_mod.lib` declares `.subckt rsil 1 2
    # bn` / `.subckt rhigh 1 2 bn` / `.subckt rppd 1 2 bn` -- each a
    # 3-terminal (two heads plus `bn`, the bulk/substrate tie) subcircuit
    # taking `w`/`l` in raw metres (e.g. `.param w=0.5e-6 l=0.5e-6`), matching
    # `decks/sg13g2.py`'s own `bulk_to_substrate=True` on all three classes
    # (so `resolve_device_bindings` already emits the matching `("A", "B",
    # "W")` terminal triple with no code change needed there) and the
    # no-`.option scale` raw-metre convention `sg13_lv_nmos`/`sg13_lv_pmos`
    # already established for this family's MOS binding (issue #1231) --
    # see `_RESISTOR_PARAM_STYLE`/`_GEOMETRY_STYLE_BY_FAMILY` below.
    #
    # `res_metal1`/`res_metal2` (also recognised by issue #1235) are
    # deliberately **not** in this table: the same fetched install's
    # `libs.tech/ngspice/` and `libs.tech/xyce/` model trees define no
    # `.subckt`/`.model` for either name at all (grepped directly, zero
    # hits) -- IHP's own reference CDL netlists (under
    # `libs.tech/klayout/tech/lvs/testing/testcases/unit/res_devices/
    # netlist/res_metal1.cdl`) instantiate them as a bare
    # semiconductor-resistor-with-model-reference card (`Rm2 net3 net4
    # res_metal1 l=1.5u w=5u`) that a consumer's own simulation deck
    # supplies the `.model res_metal1 R ...` for -- there is no real
    # subcircuit name this table could bind to. This mirrors gf180mcu's
    # `bjt` carve-out: a *documented* bare-primitive carve-out (see
    # `docs/cli/extract.md` -> "Coverage" / "Scope limits"), not an
    # oversight -- and this deck's own bare `R` card already names the
    # correct PDK device-class token (`res_metal1`/`res_metal2`) for that
    # consumer-supplied `.model` to attach to.
    ("sg13g2", "sg13g2"): {
        "rsil": "rsil",
        "rppd": "rppd",
        "rhigh": "rhigh",
    },
}

#: (deck_name, pdk_variant_family) -> {deck CapacitorDevice.name -> subckt name}.
_CAPACITOR_MODEL_TABLE: dict[tuple[str, str], dict[str, str]] = {
    ("sky130", "sky130"): {
        "sky130_fd_pr__model__cap_mim": "sky130_fd_pr__cap_mim_m3_1",
        "sky130_fd_pr__model__cap_mim_m4": "sky130_fd_pr__cap_mim_m3_2",
    },
    ("gf180mcu", "gf180mcu"): {
        "cap_mim_2f0_m4m5_noshield": "cap_mim_2f0_m4m5_noshield",
    },
}

#: (deck_name, pdk_variant_family) -> {deck BipolarDevice.class_name ->
#: ((nominal AE in um^2, subckt name), ...)}. gf180mcu is intentionally absent
#: -- its recognised ``bjt`` stays a bare ``Q`` card (documented carve-out,
#: see ``decks/gf180mcu.py:557-559`` and ``docs/cli/extract.md``).
_BIPOLAR_MODEL_TABLE: dict[
    tuple[str, str], dict[str, tuple[tuple[float, str], ...]]
] = {
    ("sky130", "sky130"): {
        "pnp": (
            (0.4624, "sky130_fd_pr__pnp_05v5_W0p68L0p68"),
            (11.56, "sky130_fd_pr__pnp_05v5_W3p40L3p40"),
        ),
    },
}

#: `DeviceClassBJT3Transistor`/`...BJT4Transistor` parameters a bound bipolar
#: binding has no call-site parameter to carry (issue #695): sky130's
#: `pnp_05v5_W0p68L0p68`/`_W3p40L3p40` subcircuits take only an optional
#: `mult` (confirmed against the real fetched install -- see the module
#: docstring's verified-provenance section), so every one of these -- all
#: independently measured by KLayout's own device extractor, verified
#: non-zero on a real synthetic BJT fixture -- is dropped when the device is
#: written as an `X` card. `AE` itself is excluded: it *is* used, to select
#: which fixed-geometry variant to call (see `_select_bipolar_variant`), so
#: it is consumed rather than silently dropped.
_BIPOLAR_DROPPED_PARAMS: tuple[str, ...] = ("PE", "AB", "PB", "AC", "PC", "NE")

#: Per-PDK-family subcircuit length/width parameter spellings (see the module
#: docstring): sky130 uses ``l``/``w``; gf180mcu's resistor/capacitor cells use
#: distinct ``r_``/``c_`` prefixes; sg13g2's `rsil`/`rppd`/`rhigh` subcircuits
#: also use plain ``l``/``w`` (confirmed in the same fetched
#: `resistors_mod.lib` cited by `_RESISTOR_MODEL_TABLE` above).
_RESISTOR_PARAM_STYLE: dict[str, tuple[str, str]] = {
    "sky130": ("l", "w"),
    "gf180mcu": ("r_length", "r_width"),
    "sg13g2": ("l", "w"),
}
_CAPACITOR_PARAM_STYLE: dict[str, tuple[str, str]] = {
    "sky130": ("l", "w"),
    "gf180mcu": ("c_length", "c_width"),
}


def resolve_device_bindings(
    deck_name: str, pdk_variant: str, deck: Any
) -> dict[str, DeviceBinding]:
    """Resolve the full ``{device-class-name -> DeviceBinding}`` map for
    ``deck`` against ``pdk_variant`` (issue #339): MOS, plus every resistor and
    capacitor class the deck declares, plus a bindable bipolar class.

    ``deck`` is the deck object (duck-typed: its ``nfet_class``/``pfet_class``,
    ``mos_flavours``, ``resistors``, ``capacitors`` and ``bipolars``
    attributes are read) -- kept a parameter rather than an import so this
    module stays free of a ``decks`` dependency.

    Raises :class:`ModelBindingError` (via :func:`resolve_mos_model_table`)
    when ``pdk_variant``'s family has no curated MOS entry for ``deck_name`` --
    the same up-front deck/PDK-mismatch guard #209 already had. A *recognised*
    device class with no curated binding of its own (today: only gf180mcu's
    ``bjt``) is left out of the map, so the writer keeps its bare primitive
    card -- a documented carve-out, never a silent wrong subcircuit call.

    Each MOS ``DeviceBinding``'s ``flavour_subckts`` (issue #1111) is
    populated from ``_MOS_MODEL_FLAVOURS`` for every ``deck.mos_flavours``
    entry with a matching table row -- a flavour the deck declares
    geometrically but this table has no curated subcircuit name for (should
    never happen for a shipped deck, but not fatal here either) simply
    contributes no override, leaving that flavour's devices bound to the
    base subcircuit, same as an unbound device class elsewhere in this
    function.
    """
    family = _pdk_variant_family(pdk_variant)
    # The target family's geometry-literal convention (issue #1396). Stamped
    # onto every binding below -- it is a property of the deck the cards are
    # written against, not of the device kind.
    geometry_style = geometry_style_for_family(family)
    mos = resolve_mos_model_table(deck_name, pdk_variant)
    mos_flavours = _MOS_MODEL_FLAVOURS.get((deck_name, family), {})
    nfet_flavour_subckts = {
        flavour: table["nfet"]
        for flavour, table in mos_flavours.items()
        if "nfet" in table
    }
    pfet_flavour_subckts = {
        flavour: table["pfet"]
        for flavour, table in mos_flavours.items()
        if "pfet" in table
    }
    bindings: dict[str, DeviceBinding] = {
        deck.nfet_class: DeviceBinding(
            "mos",
            mos["nfet"],
            ("D", "G", "S", "B"),
            flavour_subckts=nfet_flavour_subckts,
            geometry_style=geometry_style,
        ),
        deck.pfet_class: DeviceBinding(
            "mos",
            mos["pfet"],
            ("D", "G", "S", "B"),
            flavour_subckts=pfet_flavour_subckts,
            geometry_style=geometry_style,
        ),
    }

    res_table = _RESISTOR_MODEL_TABLE.get((deck_name, family), {})
    res_len, res_wid = _RESISTOR_PARAM_STYLE.get(family, ("L", "W"))
    for resistor in getattr(deck, "resistors", ()):
        subckt = res_table.get(resistor.name)
        if subckt is None:
            continue
        terminals = ("A", "B", "W") if resistor.bulk_to_substrate else ("A", "B")
        bindings[resistor.name] = DeviceBinding(
            "resistor",
            subckt,
            terminals,
            res_len,
            res_wid,
            geometry_style=geometry_style,
        )

    cap_table = _CAPACITOR_MODEL_TABLE.get((deck_name, family), {})
    cap_len, cap_wid = _CAPACITOR_PARAM_STYLE.get(family, ("L", "W"))
    for capacitor in getattr(deck, "capacitors", ()):
        subckt = cap_table.get(capacitor.name)
        if subckt is None:
            continue
        bindings[capacitor.name] = DeviceBinding(
            "capacitor",
            subckt,
            ("A", "B"),
            cap_len,
            cap_wid,
            geometry_style=geometry_style,
        )

    bjt_table = _BIPOLAR_MODEL_TABLE.get((deck_name, family), {})
    for bipolar in getattr(deck, "bipolars", ()):
        variants = bjt_table.get(bipolar.class_name)
        if variants is None:
            # gf180mcu carve-out: no curated variant table -> stays a bare Q
            # card (see `decks/gf180mcu.py:557-559` and `docs/cli/extract.md`).
            continue
        bindings[bipolar.class_name] = DeviceBinding(
            "bipolar",
            "",
            ("C", "B", "E"),
            variants=variants,
            dropped_params=_BIPOLAR_DROPPED_PARAMS,
            geometry_style=geometry_style,
        )

    return bindings


def _select_bipolar_variant(
    emitter_area_um2: float, variants: tuple[tuple[float, str], ...]
) -> str:
    """The subcircuit whose nominal emitter area is nearest ``emitter_area_um2``
    (sky130's ``pnp_05v5`` ships as discrete geometry-named cells, not a
    continuously-parameterized one -- see the module docstring)."""
    return min(variants, key=lambda entry: abs(entry[0] - emitter_area_um2))[1]


def create_model_binding_delegate(
    bindings: dict[str, DeviceBinding],
) -> kdb.NetlistSpiceWriterDelegate:
    """Build a ``kdb.NetlistSpiceWriterDelegate`` that writes any device whose
    class name is a key of ``bindings`` as an ``X`` subcircuit call per its
    :class:`DeviceBinding`, and defers every other device (an unbound
    recognised class, e.g. gf180mcu's ``bjt``, or a future deck's new class) to
    KLayout's default primitive-card behavior.

    ``import klayout.db`` is deferred to call time (mirrors every other
    KLayout import in ``extract.py``) so importing this module never pays
    the cost of loading the KLayout database module.
    """
    import klayout.db as kdb

    class _ModelBindingSpiceWriterDelegate(kdb.NetlistSpiceWriterDelegate):
        def __init__(self, mapping: dict[str, DeviceBinding]) -> None:
            super().__init__()
            self._bindings = mapping

        def _device_param(self, device: kdb.Device, name: str) -> float | None:
            for param in device.device_class().parameter_definitions():
                if param.name == name:
                    return device.parameter(param.id())
            return None

        def write_device(self, device: kdb.Device) -> None:
            device_class = device.device_class()
            binding = self._bindings.get(device_class.name)
            if binding is None:
                super().write_device(device)
                return

            # The target PDK family's geometry-literal convention (#1396):
            # bare micrometres for a deck that sets its own `.option scale`
            # (sky130), explicit unit suffixes otherwise (gf180mcu, and any
            # family with no curated entry).
            style = binding.geometry_style

            terminal_ids = {
                terminal.name: terminal.id()
                for terminal in device_class.terminal_definitions()
            }

            def net_str(terminal_name: str) -> str:
                net = device.net_for_terminal(terminal_ids[terminal_name])
                return self.net_to_string(net)

            pins = " ".join(net_str(terminal) for terminal in binding.terminals)
            name = self.format_name(device.expanded_name())

            # Per-flavour MOS subcircuit override (issue #1111): a device
            # `extract.py` tagged with `MOS_FLAVOUR_PROPERTY` (recognised
            # inside one of the deck's `mos_flavours` markers, e.g. gf180mcu's
            # `Dualgate`) rebinds to that flavour's own real subcircuit
            # instead of the class's default one -- see `DeviceBinding
            # .flavour_subckts`'s own docstring. No-op (falls through to
            # `binding.subckt` below) for an unflavoured device, or a class
            # with no declared flavours at all.
            subckt = binding.subckt
            if binding.kind == "mos" and binding.flavour_subckts:
                flavour_value = device.property(MOS_FLAVOUR_PROPERTY)
                if flavour_value is not None:
                    subckt = binding.flavour_subckts.get(str(flavour_value), subckt)

            if binding.kind == "bipolar":
                subckt = _select_bipolar_variant(
                    self._device_param(device, "AE") or 0.0, binding.variants
                )
                self.emit_line(f"X{name} {pins} {subckt}")
                return

            if binding.kind == "capacitor":
                dims = equivalent_rectangle_um(
                    self._device_param(device, "A") or 0.0,
                    self._device_param(device, "P") or 0.0,
                )
                if dims is None:
                    area = self._device_param(device, "A") or 0.0
                    side = math.sqrt(area) if area > 0.0 else 0.0
                    length_um, width_um = side, side
                else:
                    length_um, width_um = dims
            else:
                length_um = self._device_param(device, "L") or 0.0
                width_um = self._device_param(device, "W") or 0.0

            extra_params = ""
            if binding.kind == "mos":
                # Source/drain junction area+perimeter (issue #695): the
                # extractor measures these on every MOS device (see
                # `DeviceClassMOS4Transistor`'s `AS`/`AD`/`PS`/`PD`) and both
                # curated PDKs' target subcircuits declare matching call-site
                # parameters (verified against a real fetched install -- see
                # the module docstring's "Card shape" section), so nothing is
                # dropped here the way `DeviceBinding.dropped_params`
                # documents for bipolar. `AS`/`AD` are areas and `PS`/`PD`
                # lengths, each written in the binding's own
                # `geometry_style` exactly as `L`/`W` are below (#1396).
                as_um2 = self._device_param(device, "AS") or 0.0
                ad_um2 = self._device_param(device, "AD") or 0.0
                ps_um = self._device_param(device, "PS") or 0.0
                pd_um = self._device_param(device, "PD") or 0.0
                extra_params = (
                    f" AS={_format_um2(as_um2, style)}"
                    f" AD={_format_um2(ad_um2, style)}"
                    f" PS={_format_um(ps_um, style)}"
                    f" PD={_format_um(pd_um, style)}"
                )

            self.emit_line(
                f"X{name} {pins} {subckt} "
                f"{binding.length_param}={_format_um(length_um, style)} "
                f"{binding.width_param}={_format_um(width_um, style)}{extra_params}"
            )

    return _ModelBindingSpiceWriterDelegate(bindings)
