"""Extract a schematic-equivalent netlist from a GDSII/OASIS layout, headless.

Pure library: :func:`run_extract` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``drc.py``/
``sim.py``. Serialisation and human-readable formatting live in the CLI
command module (``cli/extract_cmd.py``).

This is phase 2 of Epic #153 (``klt lvs``/``klt extract``), the build carried
by the accepted spike, ``docs/design/lvs-extraction-spike.md`` -- read that
document first; it settles the engine choice (KLayout's own
``klayout.db.LayoutToNetlist``/``NetlistSpiceWriter``, already this repo's
sole runtime dependency) and the request/response contract this module
implements (its section 2a, ``klt extract``). Scope: **schematic-equivalent**
extraction only -- devices and connectivity, no parasitic R/C on the
interconnect (explicitly deferred by the spike's "Out of scope" section).

Deviation from the spike: the spike's proposed invocation is flag-only
(``klt extract <file> --deck sky130|gf180mcu``), with no PDK-resolver
involvement -- extraction, like ``klt drc``, was scoped as a whole-layout
operation against a curated rule set, not an installed-PDK operation. This
module keeps ``--deck`` as the (required) selector of the curated
connectivity + device-extraction deck (see
``klayout_tools.decks.ExtractionDeck``), self-contained exactly like `klt
drc`'s decks -- no PDK install is required to run it. It additionally accepts
optional ``--pdk``/``--pdk-root`` flags, resolved through the one shared
resolver every other PDK-aware verb uses
(:func:`klayout_tools.pdk.find_pdk`), mirroring ``klt sim``'s optional
``models.pdk``/``models.pdk_root`` resolution (see ``sim.py``): when given,
an unresolvable PDK is an application error (exit 1); when omitted,
resolution is skipped entirely and extraction runs from the curated deck
alone, so CI needs no PDK install (matching ``klt drc``'s and ``klt gen``'s
test posture -- see ``tests/test_extract.py``'s fabricated installs).

When a PDK resolves, ``--pdk``/``--pdk-root`` also change what the *written
SPICE file* looks like (not just the JSON response's provenance-only
``pdk`` field, which is all they did before issue #209): each extracted MOS
device is written as an ``X`` subcircuit call against the resolved PDK's own
curated device library, e.g. ``sky130_fd_pr__nfet_01v8``, instead of the
curated deck's bare ``nfet``/``pfet`` ``M``-card class label -- see
``klayout_tools.pdk_models`` for the curated
``(deck_name, pdk_variant_family)`` model-name table, the
``kdb.NetlistSpiceWriterDelegate`` subclass that performs the rewrite, and
the exact provenance of each bound subcircuit name. A resolved PDK whose
family/deck pairing has no curated table entry is an :class:`ExtractError`
naming what was tried -- never a silent fallback to the bare ``M``-card
form. When ``--pdk``/``--pdk-root`` are omitted, the written SPICE is
unchanged from before #209 (the bare ``M``-card form, byte-identical to the
existing golden tests).

Device recognition: each deck's ``active``/``poly``/``nwell`` layers extract
NMOS (``active - nwell``) and PMOS (``active & nwell``) via KLayout's native
``DeviceExtractorMOS4Transistor`` -- one generic ``nfet``/``pfet`` device
class per deck (no voltage-flavor distinction, e.g. no ``nfet_01v8`` vs.
``nfet_g5v0`` split), the same "curated starter subset, not the full device
zoo" scope guard ``docs/cli/drc.md`` documents for the DRC decks. A deck may
additionally declare *drawn* precision resistors (issue #222,
``klayout_tools.decks.ResistorDevice``): a conductor segment covered by the
PDK's resistor-ID layer is cut out of that conductor's connectivity region
and extracted through KLayout's native ``DeviceExtractorResistor`` /
``DeviceExtractorResistorWithBulk`` instead of being left as a short between
its two heads. See ``klayout_tools.decks.sky130``/``gf180mcu`` for the exact
per-family layer roles, the resistor sheet-resistance provenance, and their
known connectivity-fidelity limitations (well-tie handling in particular).

A deck may additionally declare one or more vertical-BJT device-recognition
entries (``ExtractionDeck.bipolars``, issue #223): the deck's ``nwell``/
``active`` layers restricted to a PDK-specific bipolar device-mark layer
(e.g. sky130's ``pnp.drawing`` 82/44, gf180mcu's ``DRC_BJT`` 127/5), fed to
KLayout's native ``DeviceExtractorBJT3Transistor``. See
:class:`klayout_tools.decks.BipolarDevice` for the layer-role contract and
:func:`_extract_netlist`'s bipolar wiring block for how the marker layer
scopes recognition to genuine device-cell instances.

A deck may likewise declare one or more junction-diode device-recognition
entries (``ExtractionDeck.diodes``, issue #542): a p-doped and an n-doped
drawn layer, each restricted to a PDK-specific diode device-mark layer (e.g.
gf180mcu's ``diode_mk`` 115/5) and narrowed by per-terminal
``requires``/``excludes`` implant layers, fed to KLayout's native
``DeviceExtractorDiode`` -- which forms the device from the two regions'
geometric overlap and reports that overlap's area/perimeter. A terminal the
PDK draws no mask for (the p-substrate side of an n+/p-substrate diode)
is declared ``None`` and tied to the deck's ``substrate_net`` global,
mirroring the collector-less bipolar case. Without such an entry a discrete
diode -- the standard ESD-clamp primitive on every pad ring -- extracts as
no device at all, so ``klt lvs`` cannot verify any diode-based clamp. See
:class:`klayout_tools.decks.DiodeDevice` for the layer-role contract.

A deck may also declare one or more drawn MiM-capacitor device-recognition
entries (``ExtractionDeck.capacitors``, issue #225): two independent
plate layers (a purpose-drawn top plate, a bottom plate on an ordinary
conductor -- optionally derived through a PDK-specific "virtual bottom
plate" sizing step) fed straight to KLayout's native
``DeviceExtractorCapacitor``. Each plate is registered as its own
self-connected node, and is wired into the rest of the deck's metal stack
per plate, where the deck declares how (issue #314): the bottom plate joins
the ``metals[]`` node whose layer its ``bottom_plate`` matches, and the top
plate joins the ``metals[]`` node named by ``top_plate_via_metal`` through
the ``top_plate_via`` layer when the deck declares both. A plate for which
the deck declares neither -- e.g. sky130's MiM top plates, whose real via
lands on a metal this curated deck does not track -- stays an isolated node:
the device and its capacitance are still extracted correctly, only that
plate's net connectivity carries the documented approximation. See
:class:`klayout_tools.decks.CapacitorDevice` for the layer-role contract, the
capacitance-per-area provenance each deck must cite, and the exact scope of
that per-plate limitation. A ``top_plate_via`` placed per the PDK's own
minimum-overlap rule for that via necessarily overlaps the bottom plate in
plan view; :func:`_exclude_capacitor_top_via_overlap` (issue #364) excludes
that overlap from the deck's generic ``vias[]`` layers before the generic
per-layer connectivity loop runs, so the DRM-legal via wires the top plate
to ``top_plate_via_metal`` without also shorting it to the bottom plate.

Every connectivity layer above (``poly``, ``contact``, ``metals``, ...) is
wired up unconditionally, regardless of whether any device extractor above
claims the geometry drawn on it: geometry for a device class the deck does
not (yet) implement is absorbed into ordinary interconnect -- a silent short
between what should be distinct terminals -- rather than skipped or flagged
(issue #288). ``warnings`` gains one narrowly-scoped heuristic diagnostic for
the most common shape of this problem, split (issue #299) into a distinct
string for "carries no declared resistor-marker layer at all" versus "carries
a marker this deck knows about, but no declared ``ResistorDevice`` claims it"
(a deck-coverage gap): see :func:`_detect_unmodelled_poly_bodies` and
``docs/cli/extract.md``'s "Known limitation: unmodelled device geometry" for
the exact signature it looks for and its documented false-negative surface.

Verified compatible with ``klt sim``'s netlist convention (see
``docs/cli/sim.md`` -> "Netlist convention"): the written SPICE is a
``.SUBCKT ... .ENDS`` circuit body with no top-level ``.control``/``.end``
card -- confirmed directly against KLayout's ``NetlistSpiceWriter`` output
(it never emits a top-level ``.END`` for a single-circuit netlist), and
exercised by ``tests/test_extract.py``.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from . import env_provenance
from ._annotation import is_reserved_annotation_layer
from ._layout import load_layout, resolve_top_cell
from ._layout import region as _region
from ._layout import texts as _texts
from ._provenance import _content_hash, _klt_version, build_provenance, sha256_file
from ._report_verify import build_check_result, build_rerun_result, get_path, hash_check
from ._report_verify import load_committed_report as _load_committed_report
from .decks import (
    BipolarDevice,
    CapacitorDevice,
    DiodeDevice,
    ExtractionDeck,
    InvalidDeckOptionError,
    ParasiticsDeck,
    ResistorDevice,
    UnknownExtractionDeckError,
    deck_source_path,
    get_extraction_deck,
    get_parasitics_deck,
)

# The cell-level (black-box + pins) abstraction subsystem lives in its own
# module (issue #1303, split out of this one at ~8800 lines). `_sanitize_
# instance_name` and `_DEF_NET_NAME_PROPERTY_ID` are re-exported (`X as X`)
# because this module also calls them directly -- `_sanitize_instance_name`
# from the parasitics-injection code below, `_DEF_NET_NAME_PROPERTY_ID` from
# `_extract_netlist`'s `--def-net-names` diagnostic -- and the test suite
# imports `_sanitize_instance_name` from `klayout_tools.extract` by name.
from .extract_abstract import _DEF_NET_NAME_PROPERTY_ID as _DEF_NET_NAME_PROPERTY_ID
from .extract_abstract import (
    _abstract_cell_mask_layers,
    _apply_def_net_name_overrides,
    _collect_abstract_instances,
    _def_net_name_probes,
    _erase_abstracted_cell_geometry,
    _load_abstract_cell_lefs,
    _local_pin_candidate_points,
    _texts_excluding_abstract_cells,
    _wire_abstract_cells,
)
from .extract_abstract import _sanitize_instance_name as _sanitize_instance_name

# The SPEF-export subsystem lives in its own module (issue #1195). Two of its
# names are re-exported here rather than merely imported: `ExtractError` (this
# package's own extraction exception -- it lives beside `_write_spef`, which
# raises it, purely to keep the dependency one-directional) and
# `def_net_instance_pins` (imported from `klayout_tools.extract` by
# `cli/extract_cmd.py`, `place_and_route.py`, and the test suite). The
# redundant `X as X` aliases mark them explicit re-exports, so neither reads as
# an unused import in a module that never calls them itself.
from .extract_spef import ExtractError as ExtractError
from .extract_spef import _write_spef
from .extract_spef import def_net_instance_pins as def_net_instance_pins
from .pdk import PdkNotFoundError, find_pdk
from .pdk_models import (
    MOS_FLAVOUR_PROPERTY,
    DeviceBinding,
    ModelBindingError,
    create_model_binding_delegate,
    equivalent_rectangle_um,
    resolve_device_bindings,
)

if TYPE_CHECKING:
    import klayout.db as kdb

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
#:
#: 2 (issue #592): the `parasitics.nets[]` entry's shape changed from one
#: shunt resistor (`internal_node`) to a star topology -- `internal_node` is
#: replaced by `hub_net` (usually the net itself now, not a fresh node) and a
#: new `terminals[]` array (one entry per device terminal moved onto the
#: star). `parasitics.r_count` also changed meaning: it now counts every
#: emitted resistor (one or more per net), not one per net, so
#: `r_count == c_count` no longer holds in general.
#:
#: 3 (issue #1376): the top-level `pdk.root` field's shape changed from a raw
#: (often absolute) filesystem path string -- the literal `--pdk-root`
#: argument, echoed verbatim -- to the `{path, scope}` shape
#: `env_provenance.repo_relative_path` already defines (mirroring `klt
#: pex`/`klt sim`/`klt size`'s own issue #1261 bump). A PDK install is
#: inherently external to whatever repo invokes `klt extract`, so the old
#: shape baked a host-specific absolute path -- possibly a username, e.g.
#: `/home/<user>/.volare/gf180mcuD` -- into any committed `--format json`
#: evidence report; `provenance.pdk` (`{name, source, version}`, no path)
#: already carries the reproducible identity of the same PDK without it. See
#: docs/cli/env-provenance.md's "external input pinned by identity, not
#: location" rationale.
SCHEMA_VERSION = 3

#: Decimal places `devices[].params` (`w_um`/`l_um`) are rounded to -- clears
#: floating-point noise from KLayout's internal dbu -> um conversion (e.g.
#: `0.14999999999999997`) without losing meaningful precision (sub-nm, well
#: below any curated deck's dbu grid).
_PARAM_PRECISION_UM = 6


def _bbox_um_rounded(box: kdb.Box, dbu: float) -> dict[str, float]:
    """Convert a ``kdb.Box`` to a JSON-serialisable ``left``/``bottom``/
    ``right``/``top`` dict, scaled from database units to micrometres by
    ``dbu`` and rounded to ``_PARAM_PRECISION_UM`` decimal places.

    Shared by the ``black_box_regions``, ``unmodelled_poly``, and
    ``dead_metal`` report entries, which all report this exact rounded
    bbox shape (issue #714).
    """
    return {
        "left": round(box.left * dbu, _PARAM_PRECISION_UM),
        "bottom": round(box.bottom * dbu, _PARAM_PRECISION_UM),
        "right": round(box.right * dbu, _PARAM_PRECISION_UM),
        "top": round(box.top * dbu, _PARAM_PRECISION_UM),
    }


#: Decimal places a drawn capacitor's `devices[].params.c_f` (in **Farads**)
#: is rounded to -- same floating-point-noise cleanup as `_PARAM_PRECISION_UM`,
#: but a MiM cap's capacitance sits in the femtofarad-to-picofarad range
#: (roughly 1e-14 to 1e-11 F for the curated decks' modelled plate sizes), so
#: clearing noise at the same *absolute* micrometre-scale precision would
#: zero out the whole value; this rounds at a correspondingly smaller
#: absolute scale instead (still far more precision than any real dbu-grid
#: geometry needs).
_PARAM_PRECISION_FARAD = 21

#: Decimal places a drawn resistor's `devices[].params.r_ohm` is rounded to
#: -- same floating-point-noise cleanup as `_PARAM_PRECISION_UM`, applied to
#: an ohms-valued parameter rather than a micrometre-valued one.
_PARAM_PRECISION_OHM = 6

#: Lower bound (ohms) clamped onto an emitted parasitic series resistor so a
#: net whose interconnect resistance rounds to ~0 still writes a well-formed,
#: simulator-safe `R` card (a literal 0-ohm resistor is a degenerate short
#: some readers reject) -- negligible against any real net's resistance.
_MIN_PARASITIC_R_OHM = 1e-3

#: SPICE's own reserved *global* ground node. Node ``0`` is ground
#: everywhere in a SPICE deck -- inside a ``.SUBCKT`` body just as much as at
#: the top level, with no ``.global`` declaration and no cooperation from the
#: instantiating testbench required. That property is what lets
#: :func:`_tie_substrate_nets_to_ground` write the substrate DC-reference
#: shunt *inside* the extracted ``.SUBCKT`` (issue #1263) without touching
#: the subcircuit's declared pin interface.
_SPICE_GLOBAL_GROUND_NODE = "0"

#: Resistance (ohms) of the DC-reference shunt `--parasitics` writes from
#: every synthesized substrate net to :data:`_SPICE_GLOBAL_GROUND_NODE`
#: (issue #1263).
#:
#: 1 Tohm is the same order as ngspice's own ``.options rshunt`` remedy for a
#: floating node (the workaround issue #1263 was reported with), chosen to be
#: large enough that it is electrically invisible -- 1.8 pA at a 1.8 V rail,
#: ~13 orders of magnitude below any current a real extracted device carries,
#: and an RC time constant of ~0.3 s against the ~0.3 fF of substrate
#: capacitance a small cell's extraction produces (i.e. eight-plus orders
#: slower than any transient a post-layout testbench sweeps). It is a
#: numerical anchor for the DC solve, not a circuit element.
SUBSTRATE_DC_TIE_RESISTANCE_OHM = 1e12

#: Machine-readable declaration of what `--parasitics` does and does not
#: model (issue #728, updated by #760). Most `C` cards `_inject_parasitics`
#: emits hang off the deck's ground/substrate net; since issue #760 a net
#: pair with vertical-overlap (crossover) coupling also gets a direct
#: net-to-net `C` card between their hub nodes -- but nothing in the emitted
#: netlist or JSON said what *was* and was not modelled before this field
#: existed. This is a static description of the model itself (it does not
#: vary net-to-net, deck-to-deck, or run-to-run), so it is a single
#: module-level constant reused verbatim by both the `parasitics.model` JSON
#: field (`run_extract`) and the written SPICE netlist's header comment
#: (`_parasitic_model_header_comment`) -- one statement of the model's
#: limits, not two that could drift apart. See "Parasitic model scope
#: (`parasitics.model`)" in docs/cli/extract.md.
PARASITIC_MODEL_SCOPE: dict[str, str] = {
    "capacitance": (
        "net-to-ground for every net's own (non-coupled) area/perimeter, "
        "plus net-to-net for the vertical-overlap coupling `coupling` "
        "describes below -- a coupled net pair gets a direct capacitor "
        "between their two hub nodes, not just capacitors to the deck's "
        "ground/substrate net"
    ),
    "coupling": (
        "vertical overlap (crossover) unconditionally -- where one net's "
        "conductor on an adjacent metal level sits directly over another "
        "*distinct* net's conductor, that overlap area is charged between "
        "the two nets instead of to ground (issue #760) -- plus lateral "
        "(same-layer, sidewall) coupling, but only for a net pair naming "
        "one of the caller's declared `--critical-net` nets (issue #976): "
        "facing-edge length within that layer's own minimum-spacing "
        "lookback is charged between the two nets, *additively* (not "
        "deducted from either net's substrate fringe term, unlike the "
        "vertical case -- a known simplification). Any same-layer pair "
        "with neither side named `--critical-net`, and fringe shielding in "
        "general, are still not modelled"
    ),
    "resistance": (
        "single lumped series resistance per net, distributed as a star "
        "across that net's device terminals (issue #592) -- not a "
        "per-segment, distributed RC ladder, *unless* `--distributed-rc` "
        "names this net via `--critical-net` (issue #977, Epic #709 Phase "
        "2b): then the net's terminals are ordered along their approximate "
        "physical spread and its total R/C is broken into a chain of "
        "per-segment resistors (segment length proportional to inter-"
        "terminal distance) with a ground capacitor at each terminal node "
        "(proportional to its adjacent segment length/2), instead of one "
        "star hub -- still an approximation (terminal position is a "
        "device-placement proxy, not true per-segment routing geometry), "
        "but a strictly finer-grained one than the single-hub star"
    ),
    "frequency": (
        "quasi-static -- one frequency-independent R and C per net; no "
        "skin effect, no distributed transmission-line behavior"
    ),
}


def _parasitic_model_header_comment() -> str:
    """Render :data:`PARASITIC_MODEL_SCOPE` as `*`-prefixed SPICE comment
    lines for the written netlist's header (issue #728).

    ``kdb.Netlist.write``'s ``description`` argument only `*`-comments its
    *first* line -- every subsequent line is written verbatim, which would
    otherwise land as unprefixed text a SPICE reader could try to parse as a
    circuit element. Each line here is pre-prefixed with ``* `` so the whole
    block stays a comment regardless of how many lines it spans. Called only
    when ``--parasitics`` was given (``parasitics_report is not None`` in
    ``run_extract``) -- a netlist with no parasitics has nothing to declare
    the scope of.
    """
    lines = ["* parasitic model (--parasitics):"]
    for key, value in PARASITIC_MODEL_SCOPE.items():
        lines.append(f"* - {key}: {value}")
    return "\n".join(lines)


#: Relative permittivity assumed for the `klt mom` cross-check
#: (`--mom-net`, issue #798): SiO2's textbook value, the same
#: ``background_permittivity`` `docs/cli/mom.md`'s own worked examples use.
#: The cross-check needs *some* single value (the deck's curated
#: ``cap_area_ff_um2``/``cap_perim_ff_um`` coefficients do not themselves
#: carry a declared permittivity -- see `_mom_ground_capacitance_for_net`'s
#: docstring for how it is used to invert an implied z-gap from them), and
#: this keeps it identical to `klt mom`'s own existing convention rather
#: than inventing a second one.
MOM_CROSSCHECK_BACKGROUND_PERMITTIVITY = 3.9

#: Vacuum permittivity, F/m (CODATA 2018) -- mirrors
#: `tests/test_mom_validation.py`'s own `EPS0_F_PER_M`, the constant this
#: repo's `klt mom` validation already grades the solver against.
_EPS0_F_PER_M = 8.854_187_812_8e-12

#: How far beyond a net shape's own bounding box the synthesized ground
#: plate (`_mom_ground_capacitance_for_net`) extends, as a multiple of that
#: shape's own implied z-gap. A ground plate exactly the same size as the
#: net's own footprint loses a large share of the net's edge field lines to
#: open space rather than terminating them on the plate below -- the deck's
#: own `cap_area_ff_um2` coefficient implicitly assumes an effectively
#: infinite plane, the same assumption a real PDK's field-solver-derived
#: coefficient table is built from (see `docs/design/extract-fidelity-
#: roadmap.md` section 2.2). `3.0` is not a free parameter tuned to match
#: any particular net -- it was chosen from a convergence sweep run during
#: this feature's implementation (issue #798's PR description records the
#: measured numbers): the self-capacitance of a representative
#: single-shape net moves by less than 0.3% between `2x` and `5x` padding
#: at a fixed panel size, so `3x` sits solidly in the "additional padding no
#: longer changes the answer" regime without materially growing the panel
#: count.
_MOM_CROSSCHECK_GROUND_PAD_FACTOR = 3.0


def _mom_crosscheck_gap_um(cap_area_ff_um2: float, eps_r: float) -> float:
    """Invert the parallel-plate formula ``C/A = eps0 * eps_r / d`` to the
    z-gap ``d`` (in um) a PDK's own ``cap_area_ff_um2`` coefficient implies
    at relative permittivity ``eps_r``.

    This is the exact formula ``tests/test_mom_validation.py``'s
    ``parallel_plate_ff`` (the closed form `klt mom` is graded against)
    computes capacitance *from* -- ``parallel_plate_ff(area, gap, eps_r) ==
    EPS0_F_PER_M * eps_r * (area / gap) * 1e9``, i.e. ``C/A == EPS0_F_PER_M *
    1e9 * eps_r / gap``, solved here for ``gap`` given a *known* ``C/A``
    (the deck's own curated coefficient) instead of a known ``gap``. It does
    not claim to recover the real physical li1/met1-to-substrate distance
    (this repo's decks curate no such stackup height -- see
    :class:`~klayout_tools.decks.ParasiticsDeck`); it is the spacing an
    idealised, infinite parallel plate at relative permittivity ``eps_r``
    would need to reproduce that coefficient's area term. See
    `_mom_ground_capacitance_for_net`'s docstring for how it is used.
    """
    return _EPS0_F_PER_M * 1e9 * eps_r / cap_area_ff_um2


def _mom_ground_capacitance_for_net(
    l2n: kdb.LayoutToNetlist,
    net: kdb.Net,
    dbu: float,
    parasitics_deck: ParasiticsDeck,
    metal_index: list[int],
    background_permittivity: float = MOM_CROSSCHECK_BACKGROUND_PERMITTIVITY,
) -> dict[str, Any]:
    """Cross-check one net's lumped-RC ground capacitance against `klt
    mom`'s Method-of-Moments field solver (issue #798, Phase 1b of epic
    #701) -- ``klt extract --mom-net <net>``'s implementation.

    For each of the deck's ``metals`` roles with a curated
    :class:`~klayout_tools.decks.LayerRC` and non-empty geometry on
    ``net`` (read via ``l2n.polygons_of_net``, the same per-net/per-layer
    API :func:`_compute_parasitics` uses), every constituent shape's
    axis-aligned bounding box becomes one `klt mom` conductor panel for
    ``net``, at an idealised z-height derived from *that role's own*
    ``cap_area_ff_um2`` coefficient (see :func:`_mom_crosscheck_gap_um`) --
    a role with a larger area-capacitance coefficient implies a closer
    (smaller-gap) plane, a smaller coefficient a farther one, exactly the
    inverse relationship the parallel-plate formula states. A second
    conductor, ``"gnd"``, is synthesized directly beneath each such shape:
    a plate at ``z=0`` covering that shape's own bbox padded by
    :data:`_MOM_CROSSCHECK_GROUND_PAD_FACTOR` times its implied gap in every
    direction (see that constant's docstring for why a same-size plate
    underestimates the capacitance a real, effectively-infinite ground
    plane would show). `klt mom`'s solver is then run on this synthesized
    two-conductor request directly (:func:`~klayout_tools.mom.
    solve_capacitance_matrix`, no GDS/spec-file round trip), and the
    ``"net"``/``"net"`` diagonal of the returned Maxwell capacitance matrix
    -- the capacitance between ``net`` and ``gnd`` in this isolated
    two-conductor system, the same reading `docs/cli/mom.md`'s own
    parallel-plate worked example uses -- is this net's MoM-derived ground
    capacitance.

    **Scope, stated plainly (this is a cross-check, not a general-purpose
    field solve):** only the deck's ``metals`` roles are modelled -- a net
    whose lumped-RC ground capacitance also draws on the ``poly``/
    ``diffusion`` roles (when a deck curates them; sky130 curates no
    ``diffusion`` role at all, see ``decks/sky130.py``) is not fully
    represented here, and this function always says so in its returned
    ``warnings``. The synthesized ``gnd`` plate is a *modelling choice*
    (an idealised infinite-plane stand-in), not a measurement of this
    layout's real substrate/well geometry -- exactly the same idealisation
    the lumped-RC coefficient it is compared against already makes (see
    `docs/design/extract-fidelity-roadmap.md` section 2.2's description of
    how a PDK's own area/fringe coefficient table is derived).

    Returns ``{"net", "net_id", "mom_capacitance_ff",
    "background_permittivity", "panel_size_um", "panel_count",
    "ground_pad_factor", "warnings"}``. ``net_id`` is ``net.cluster_id`` --
    the identity of the exact net *object* whose geometry was solved, which
    ``net`` (a layout label) does **not** pin down: several genuinely
    distinct, un-strapped islands can share one label (issue #765/#811), so
    the caller resolves this solve back to its ``_compute_parasitics`` ground
    entry by ``net_id`` rather than re-matching by name (see
    :func:`_mom_ground_entry_for_crosscheck`).
    ``mom_capacitance_ff`` is ``None`` (with an explanatory ``warnings``
    entry, never a silent zero) when ``net`` has no ground-eligible geometry
    on any curated ``metals`` role. Raises :class:`~klayout_tools.mom.
    MomError` (via ``solve_capacitance_matrix``) for a missing/unbuilt
    ``klt_mom_native`` extension or a solver-level failure (a singular
    matrix, or the panel-count guard) -- the caller (`run_extract`) turns
    that into a clean :class:`ExtractError`, matching how every other
    engine-dependency failure in this module is surfaced.
    """
    from . import mom as mom_module

    net_boxes: list[dict[str, float]] = []
    gnd_boxes: list[dict[str, float]] = []
    min_gap_um: float | None = None
    warning_notes = [
        "the `--mom-net` cross-check covers only the deck's `metals` roles "
        "(e.g. li1..met5 for sky130) -- a `poly`/`diffusion` ground-"
        "capacitance role, when curated, is not included in this "
        "comparison, and the synthesized ground plate is an idealised "
        "stand-in, not a measurement of this layout's real substrate/well "
        "geometry; see docs/cli/extract.md's `--mom-net` section"
    ]

    for i, layer_rc in enumerate(parasitics_deck.metals):
        if layer_rc is None or i >= len(metal_index):
            continue
        region = l2n.polygons_of_net(net, metal_index[i])
        if region.is_empty():
            continue
        gap_um = _mom_crosscheck_gap_um(
            layer_rc.cap_area_ff_um2, background_permittivity
        )
        min_gap_um = gap_um if min_gap_um is None else min(min_gap_um, gap_um)
        pad_um = _MOM_CROSSCHECK_GROUND_PAD_FACTOR * gap_um
        region.merged_semantics = False
        for polygon in region.each():
            box = polygon.bbox()
            x0_um = box.left * dbu
            y0_um = box.bottom * dbu
            x1_um = box.right * dbu
            y1_um = box.top * dbu
            net_boxes.append(
                {
                    "x0_um": x0_um,
                    "y0_um": y0_um,
                    "x1_um": x1_um,
                    "y1_um": y1_um,
                    "z0_um": gap_um,
                    "z1_um": gap_um,
                }
            )
            gnd_boxes.append(
                {
                    "x0_um": x0_um - pad_um,
                    "y0_um": y0_um - pad_um,
                    "x1_um": x1_um + pad_um,
                    "y1_um": y1_um + pad_um,
                    "z0_um": 0.0,
                    "z1_um": 0.0,
                }
            )

    if not net_boxes or min_gap_um is None:
        return {
            "net": spice_safe_net_name(net.expanded_name()),
            "net_id": net.cluster_id,
            "mom_capacitance_ff": None,
            "background_permittivity": background_permittivity,
            "panel_size_um": None,
            "panel_count": None,
            "ground_pad_factor": _MOM_CROSSCHECK_GROUND_PAD_FACTOR,
            "warnings": [
                "net has no ground-eligible geometry on any of the deck's "
                "curated `metals` roles -- no `klt mom` cross-check is "
                "possible"
            ],
        }

    panel_size_um = min(mom_module.DEFAULT_PANEL_SIZE_UM, min_gap_um / 4.0)
    try:
        response = mom_module.solve_capacitance_matrix(
            [
                {"name": "net", "boxes": net_boxes},
                {"name": "gnd", "boxes": gnd_boxes},
            ],
            background_permittivity,
            panel_size_um=panel_size_um,
        )
    except mom_module.MomError as exc:
        # Re-raised as `ExtractError` (this module's own engine-failure
        # exception) rather than left as `MomError`: every other
        # engine-dependency failure `run_extract` can hit (an unresolvable
        # PDK, a bad deck) surfaces as `ExtractError`, and `run_extract`
        # does not otherwise know to catch `MomError` -- see this function's
        # docstring.
        raise ExtractError(f"--mom-net cross-check failed: {exc}") from exc
    net_index = response["conductors"].index("net")
    mom_capacitance_ff = response["capacitance_matrix_ff"][net_index][net_index]

    return {
        "net": spice_safe_net_name(net.expanded_name()),
        "net_id": net.cluster_id,
        "mom_capacitance_ff": round(mom_capacitance_ff, 6),
        "background_permittivity": background_permittivity,
        "panel_size_um": round(panel_size_um, 6),
        "panel_count": response["panel_count"],
        "ground_pad_factor": _MOM_CROSSCHECK_GROUND_PAD_FACTOR,
        "warnings": warning_notes + list(response["warnings"]),
    }


def _mom_ground_entry_for_crosscheck(
    ground_nets: list[dict[str, Any]], mom_crosscheck: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Return the :func:`_compute_parasitics` ground entry belonging to the
    exact net object :func:`_mom_ground_capacitance_for_net` solved, or
    ``None`` if that net carries no ground-eligible parasitics geometry.

    **Resolved by ``net_id`` (``Net.cluster_id``), never by name (issue
    #811)** -- the same rule :func:`_inject_parasitics` already follows for
    issue #765. A ``--mom-net`` *name* does not identify a net object:
    several genuinely distinct, electrically unconnected islands can carry
    one layout label (the ``gcd`` corpus block has 105 separate un-strapped
    ``VGND`` islands, 88 ``VPWR``). The solver measures one specific island's
    geometry, while a name-keyed lookup over the ``(net, net_id)``-sorted
    ground list would always return the *smallest-``net_id``* entry sharing
    that label -- so for any duplicated label the solved island's
    capacitance could be written onto a different island's SPICE ``C`` card
    and ``parasitics.nets[]`` entry, with the reported
    ``lumped_rc_capacitance_ff``/``delta_ff`` comparing two different pieces
    of geometry. Keying both halves on the id the solve already carries makes
    them agree by construction rather than by iteration-order coincidence.
    """
    net_id = mom_crosscheck["net_id"]
    return next((entry for entry in ground_nets if entry["net_id"] == net_id), None)


def run_extract(
    path: str,
    deck_name: str,
    output: str | None = None,
    top: str | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    parasitics: bool = False,
    top_cell_pins_only: bool = False,
    declared_pins: frozenset[str] | None = None,
    apply_resistor_fixed_offset: bool = True,
    deck_options: Mapping[str, str] | None = None,
    abstract_cell_patterns: tuple[str, ...] = (),
    abstract_cell_lef_paths: tuple[str, ...] = (),
    mom_net: str | None = None,
    mom_background_permittivity: float = MOM_CROSSCHECK_BACKGROUND_PERMITTIVITY,
    spef_output: str | None = None,
    def_net_names: bool = False,
    critical_nets: Sequence[str] | None = None,
    distributed_rc: bool = False,
    def_net_connections: Mapping[str, Sequence[tuple[str, str]]] | None = None,
    mom_rlc_net: str | None = None,
    mom_rlc_resistance_ohm: float | None = None,
    mom_rlc_capacitance_ff: float | None = None,
    mom_rlc_inductance_nh: float | None = None,
    matched_device_groups: Mapping[str, Sequence[str]] | None = None,
    def_pins: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Extract a schematic-equivalent netlist from the layout at ``path``.

    ``deck_name`` selects the curated :class:`~klayout_tools.decks.ExtractionDeck`
    (currently ``"sky130"``/``"gf180mcu"``). ``output`` overrides the written
    SPICE path (default: ``path`` with its extension replaced by
    ``.spice``, next to the input -- the "next to the input" convention
    ``klt render``/``klt sim`` already use). ``top`` selects the top cell
    when the stream has more than one (required in that case; otherwise
    optional and must name the sole top cell if given).

    ``deck_options`` (``klt extract --deck-option <key>=<value>``, repeatable;
    issue #595) selects which caller-visible **sheet-rho flavour** of a
    resistor family whose members share *identical* recognition geometry is
    wired for this run -- e.g. gf180mcu's ``Resistor``-marked poly family,
    whose real PDK LVS deck selects one of ``1k``/``2k``/``3k`` via a
    build-time ``POLY_RES`` variable rather than any drawn layer. ``None`` or
    an empty mapping (the default) resolves the deck exactly as before this
    parameter existed. See
    :func:`~klayout_tools.decks.get_extraction_deck`'s and
    :class:`~klayout_tools.decks.ResistorDevice`'s own docstrings for the
    full mechanism, and ``docs/cli/extract.md``'s "Selecting a shared-geometry
    resistor flavour" section for the CLI contract. A key/value this deck's
    declared resistors do not recognise is an :class:`ExtractError` (wrapping
    :class:`~klayout_tools.decks.InvalidDeckOptionError`), not a silent no-op
    or a silently-kept default. The resolved mapping is echoed verbatim in
    the response's ``provenance.deck.options`` so a record can pin exactly
    which flavour a run selected.

    ``top_cell_pins_only`` (the ``--top-cell-pins`` flag) controls how
    labelled nets become top-level pins (issue #291). Extraction is flat, so
    ``make_top_level_pins()`` would otherwise promote *every* named net --
    including nets that are only named because a label sits inside an
    instanced sub-cell, which are ordinary internal nodes once instanced.
    When ``True``, only labels drawn directly in the top cell are promoted to
    pins; a net named solely by a label found below an instance boundary
    keeps its name but stays internal. Independent of the flag, a
    ``warnings`` entry is emitted whenever such a below-top label was
    promoted (default) or kept internal (flag set), so the promotion is
    always visible rather than silently inferred from an unexpected pin
    count. Off by default, so a flat layout (or any layout whose pin labels
    all live in the top cell) is byte-for-byte unchanged.

    ``declared_pins`` (the ``--pins`` flag, issue #514) is a per-*net*
    declaration of the intended interface, orthogonal to
    ``top_cell_pins_only``'s per-*cell* one: when given (a non-``None`` set
    of net names), every promoted pin whose net name is *not* in the set is
    demoted back to an internal net -- it keeps its name, it is simply not
    exposed as a top-level pin. This is the fix for labelling an internal
    node of a lumped schematic device (e.g. one tap of a metal-option
    ladder modelled as a single series device) purely for documentation --
    today that label always promotes the node to a pin, which blocks ``klt
    lvs``'s ``options.combine_devices`` from folding the series chain the
    reference netlist models as one device. Reuses
    :func:`_reconcile_top_pins` exactly as ``top_cell_pins_only`` does, with
    a different demote-set: every currently-promoted pin name minus
    ``declared_pins``. A ``warnings`` entry lists any net demoted this way,
    and a separate entry lists any declared name that matched no promoted
    net (a likely typo, not silently ignored). ``None`` (the default) skips
    this reconciliation entirely -- byte-identical to today's behavior, same
    invariant ``top_cell_pins_only``'s own default preserves. Applied after
    ``top_cell_pins_only``'s own reconciliation, and only ever *further*
    demotes -- it cannot re-promote a net ``top_cell_pins_only`` already
    kept internal.

    ``def_pins`` (the ``--def-pins`` flag, issue #1390) is the **automatic**
    counterpart to ``declared_pins``, for a layout produced by ``klt
    place-and-route``'s own DEF->GDS merge. That merge flattens the whole
    design into the top cell, so DEF's ``NETS``-section connection points
    end up geometrically indistinguishable from a genuine DEF
    ``PINS``-section top-level port -- ``top_cell_pins_only``'s below-top
    set is always empty against such a layout (see its own paragraph
    above), so it cannot help here (issue #1385's own documented gap).
    ``def_pins`` is the design's genuine top-level port *net* names, parsed
    directly off a routed DEF's own ``PINS`` section
    (``place_and_route.def_pin_names``, the CLI's ``--def-pins <path>``) --
    the same data source ``declared_pins`` needs a caller to have separately
    derived and passed by hand.

    A caller cannot reuse ``declared_pins``'s plain exact-string match for
    this: KLayout's flat extraction joins every distinct text label found on
    one electrical net into a single, comma-separated ``Net.name`` (see
    :func:`spice_safe_net_name`'s docstring), and in a densely-routed
    DEF-merged layout *most* nets -- port or not -- carry two or more such
    labels once routing connects a driver's local output-pin label to a
    receiver's local input-pin label (or, for a genuine port, the DEF
    ``PINS``-declared label to whichever local pin it connects into)
    -- exactly the "collided, comma-joined names" #1390's own issue text
    describes (measured on this repo's own routed `gcd` corpus fixture: 494
    of 752 promoted pins carry 2+ joined labels). So a promoted net is kept
    when *any* of its comma-joined component labels is in ``def_pins``, not
    only when the whole joined name matches verbatim -- every other
    currently-promoted net is demoted, exactly as ``declared_pins`` demotes
    on a plain miss. A ``warnings`` entry lists any net demoted this way,
    and a separate entry lists any ``def_pins`` name that matched no
    promoted net's label set. Applied *after* ``declared_pins``'s own
    reconciliation (when both are given), so it can only further restrict.
    ``None`` (the default) skips this reconciliation entirely --
    byte-identical to today's behavior. See ``docs/cli/extract.md``'s
    "DEF-derived declared pins" section for a worked example against the
    real `gcd` corpus fixture.

    Two additional cause-agnostic ``warnings`` entries (issue #1385) fire
    independent of any flag above: one when the layout carries zero text on
    any of ``deck``'s own label layers anywhere in the cell tree (no net can
    be named at all -- the observed real-world trigger is a ``klt
    place-and-route`` request whose ``io.layer_h``/``io.layer_v`` choice
    lands on a GDS layer ``deck`` never scans for pin labels), and one after
    every promotion/demotion pass above has run, when the top circuit ends
    up with zero top-level pins regardless of cause. Both exist because
    ``klt lvs``'s ``NetlistComparer`` has no net/device anchor to seed
    correspondence with zero top-level pins, and reports a full mismatch
    with no hint the root cause is upstream pin promotion rather than device
    extraction.

    ``apply_resistor_fixed_offset`` (issue #559/#585, exposed on the CLI as
    ``klt extract --defer-resistor-fixed-offset`` by issue #588): when
    ``True`` (the default, the behavior every existing caller and every
    ``klt extract`` invocation without that flag gets), each opted-in
    resistor device class's
    :attr:`~klayout_tools.decks.ResistorDevice.fixed_offset_ohm` head/end
    term is added to ``R`` once per drawn primitive at extraction time --
    baked into both the written SPICE and the JSON ``devices[].params``.
    Passing ``False`` **defers** that correction: the returned netlist (and
    the written SPICE) carry only the raw per-primitive body ``R``. This is
    for a caller who intends to read the netlist back through ``klt lvs``'s
    ``layout.netlist`` + ``layout.deck`` + ``options.combine_devices: true``
    shape, where the correction must be applied exactly *once per
    post-combine logical device* rather than once per primitive -- applying
    it here first would double-count it after the series fold (issue #585).
    Mirrors how ``lvs.py``'s inline-extraction path already defers the
    correction internally via ``extract_netlist_from_layout``.

    ``pdk_variant``/``pdk_root`` (the ``--pdk``/``--pdk-root`` flags) are
    optional: when either is given, the PDK is resolved via
    :func:`klayout_tools.pdk.find_pdk` and an unresolvable PDK is an
    :class:`ExtractError`; when both are omitted, resolution is skipped
    entirely (see the module docstring's "Deviation from the spike").

    ``mom_net`` (``klt extract --mom-net <net>``, issue #798, requires
    ``parasitics=True``) names exactly one net whose ground capacitance is
    cross-checked against `klt mom`'s Method-of-Moments field solver (see
    :func:`_mom_ground_capacitance_for_net`) instead of the deck's lumped-RC
    coefficient table -- Epic #701 Phase 1b's "wire `klt mom` as a
    high-fidelity `klt extract` backend for one critical net." The MoM value
    *replaces* that one net's ``capacitance_ff`` in both the written SPICE
    ``C`` card and the JSON ``parasitics.nets[]`` entry (every other net is
    unaffected); the pre-swap lumped-RC value and the measured delta between
    the two are reported in the new ``parasitics.mom_crosscheck`` block --
    see ``docs/cli/extract.md``'s ``--mom-net`` section for the full field
    list and a worked example. Requires the ``klt_mom_native`` extension to
    be built (see ``docs/cli/mom.md#building-the-native-extension``); an
    unbuilt extension, a name matching no net with ground-eligible
    parasitics geometry, or a solver-level failure is an
    :class:`ExtractError` (this is an explicit request for a specific net's
    value, not a best-effort diagnostic -- unlike, say, ``metals_without_
    coefficient``, silently falling back to the lumped-RC value would hide
    exactly the failure a caller invoking this flag wants to know about).
    A ``mom_net`` name shared by several genuinely distinct, un-strapped
    net islands (issue #811 -- the ``gcd`` corpus block has 105 same-labelled
    ``VGND`` islands) solves the **lowest-``net_id``** one, i.e. the first
    entry carrying that name in ``parasitics.nets[]``, reports which island
    that was as ``mom_crosscheck.net_id``, warns that the name was
    ambiguous, and leaves every other same-named island's lumped-RC value
    untouched. The entry whose ``capacitance_ff`` is overwritten is resolved
    from that same ``net_id`` (:func:`_mom_ground_entry_for_crosscheck`), so
    the swapped island and the measured island are the same one by
    construction rather than by iteration-order coincidence.
    ``mom_background_permittivity`` (not currently exposed as its own CLI
    flag) overrides :data:`MOM_CROSSCHECK_BACKGROUND_PERMITTIVITY`, the
    relative permittivity assumed when inverting each metal role's
    ``cap_area_ff_um2`` coefficient to a z-gap for the solve. ``mom_net``
    omitted (the default) skips this entirely -- byte-identical to before
    this feature existed.

    ``spef_output`` (``klt extract --spef <path>``, issue #948, Epic #700
    Phase 3) additionally writes ``parasitics``'s per-net R/C model as a
    Standard Parasitic Exchange Format file at the given path -- see
    :func:`_write_spef` for the exact translation and its documented
    net-name-only correlation scope, and ``docs/cli/extract.md``'s "SPEF
    export" section for the CLI contract. Requires ``parasitics=True``
    (there is nothing to translate otherwise); given without it, this is an
    :class:`ExtractError`, the same "a flag naming something invalid is an
    error, not a silent no-op" convention ``mom_net`` above already follows.
    ``None`` (the default) skips this entirely -- byte-identical to before
    this feature existed. The resolved path is echoed back as the response's
    ``spef_path`` field (``null`` when omitted).

    ``def_net_names`` (``klt extract --def-net-names``, issue #951, Epic #700
    Phase 3) names each routed net from the **DEF net name** KLayout's LEF/DEF
    reader stored on its geometry as GDS shape property
    :data:`_DEF_NET_NAME_PROPERTY_ID`, instead of from GDS text labels, for
    every net that carries one. On a routed GDS from ``klt place-and-route``
    this replaces KLayout's synthesised ``$<id>`` placeholders and
    pin-label-joined ``A,X`` names with the design's own ``_019_`` /
    ``req_msg[3]`` names -- which is what makes the emitted SPICE/SPEF net
    names line up with the netlist an STA tool has linked (see
    ``klayout_tools.place_and_route``'s ``post_route_spef``). Off by default:
    property ``1`` carries no guaranteed meaning in a GDS that did *not* come
    from a LEF/DEF merge, so this is opt-in rather than inferred, and every
    other layout's output is byte-identical to before this flag existed. A
    run that opts in and finds no such property says so in ``warnings``
    rather than silently changing nothing.

    ``critical_nets`` (``klt extract --critical-net <net>``, repeatable,
    issue #976, Epic #709 Phase 2a) additionally computes lateral
    (same-layer, sidewall) coupling capacitance for any same-layer net pair
    naming one of these nets -- the increment beyond issue #760's
    vertical-overlap-only coupling that ``docs/cli/pex.md``'s "Relationship
    to Epic #709's later phases" section named as Phase 2+ scope. See
    :func:`~klayout_tools.extract._compute_parasitics`'s docstring for the
    exact geometry and why it is scoped to a caller-declared net set rather
    than computed unconditionally like the vertical case. Requires
    ``parasitics=True`` (there is no lumped-RC pass to layer coupling onto
    otherwise); given without it, this is an :class:`ExtractError`, the same
    convention ``mom_net``/``spef_output`` above already follow. A name
    matching no net with ground-eligible parasitics geometry is not an
    error (unlike ``mom_net``) -- it is reported in ``warnings`` instead,
    since a caller may legitimately name several candidate nets across
    several blocks/runs. ``None``/empty (the default) skips this entirely --
    byte-identical to before this feature existed.

    ``distributed_rc`` (``klt extract --distributed-rc``, issue #977, Epic
    #709 Phase 2b) replaces the single-lumped-element star/Gamma-shunt R/C
    model (see :func:`_inject_parasitics`) with a distributed, multi-segment
    RC ladder for every net named in ``critical_nets`` -- the same
    caller-declared "nets that matter" set ``critical_nets`` already scopes
    lateral coupling onto, reused rather than inventing a second net
    classification mechanism (Epic #709 Phase 2's own framing: high-
    impedance nodes, a SAR ADC's CDAC top plate, a PLL loop filter). See
    :func:`_inject_parasitics`'s and :func:`_distributed_rc_segments`'s
    docstrings for the exact per-segment/per-node derivation. Requires
    ``critical_nets`` to be non-empty (there is no net set to scope this
    onto otherwise); given without it, this is an :class:`ExtractError`, the
    same "a flag naming something invalid is an error" convention
    ``mom_net``/``spef_output``/``critical_nets`` above already follow. A
    named net with fewer than 2 device terminals (nothing to chain into a
    ladder) silently keeps the star/Gamma-shunt model -- not an error, since
    a caller may legitimately name several candidate nets, only some of
    which end up with 2+ terminals in a given layout. ``False`` (the
    default) skips this entirely -- byte-identical to before this feature
    existed.

    ``def_net_connections`` (issue #961, Epic #700 Phase 3) is
    :func:`def_net_instance_pins`'s own ``{net_name: ((inst, pin), ...)}``
    mapping, parsed from the routed DEF's own ``NETS`` section -- only
    meaningful together with ``spef_output`` (there is nowhere else this
    data is used). Threaded straight through to :func:`_write_spef`'s
    ``net_instance_pins`` parameter, whose docstring documents the exact
    ``*CONN``/``*RES`` shape it produces and the duplicate-net-name guard
    that skips it. ``None`` (the default) is byte-identical to the pre-#961
    port-only ``*CONN`` behavior.

    ``mom_rlc_net``/``mom_rlc_resistance_ohm``/``mom_rlc_capacitance_ff``/
    ``mom_rlc_inductance_nh`` (``klt extract --mom-rlc-net <net>
    --mom-rlc-resistance-ohm <r> --mom-rlc-capacitance-ff <c>
    [--mom-rlc-inductance-nh <l>]``, issue #988, Epic #709 Phase 3a)
    substitute a caller-supplied, directly-solved R/L/C for one named net --
    e.g. from a separate ``klt mom`` (Method-of-Moments, Epic #701) run
    against that net's real geometry -- in place of this function's own
    Phase 1/2 lumped-RC ground model for that net, so a later ``klt sim``/
    ``klt pex`` re-simulation reflects a MoM-grade parasitic on exactly the
    net a caller has singled out as critical. Unlike ``mom_net`` above
    (which drives its own internal, idealised-ground-plate MoM solve and
    reports the comparison), this is a pure value substitution: the three
    numeric overrides are opaque to this function -- it does not call `klt
    mom` itself, does not know or care how they were derived, and applies
    exactly the ones given (each independently optional; a caller trusting
    MoM for capacitance only, say, can omit ``mom_rlc_resistance_ohm`` and
    keep the lumped-RC value for that component). Requires
    ``mom_rlc_net`` whenever any of the three values is given (and vice
    versa), and requires ``parasitics=True`` -- there is no per-net R/C
    entry to substitute into otherwise; either violation is an
    :class:`ExtractError`, the same "a flag naming something invalid is an
    error" convention every other opt-in flag above follows. A
    ``mom_rlc_net`` matching no net with ground-eligible parasitics geometry
    in this layout is also an :class:`ExtractError` (unlike
    ``critical_nets``' tolerant "reported in warnings" convention -- a
    caller supplying a real measured value for a specific net expects it
    applied, not silently skipped). Mutually exclusive with
    ``distributed_rc`` naming the same net (via ``critical_nets``) -- a
    caller-supplied lumped R/C total and a multi-segment ladder derived from
    the deck's own coefficient table cannot both describe one net's model at
    once; combining them for the same net is an :class:`ExtractError`.
    ``mom_rlc_resistance_ohm``/``mom_rlc_capacitance_ff`` replace this net's
    ``_compute_parasitics`` ground-list entry/entries (every net *object*
    sharing this net *name* -- e.g. several un-strapped islands with the
    same layout label -- gets the same override) before
    :func:`_inject_parasitics` reads them, exactly where ``mom_net``'s own
    swap happens, so both the written SPICE ``R``/``C`` cards and the
    ``parasitics.nets[]`` entry/entries for this net carry the substituted
    value. ``mom_rlc_inductance_nh``, when given, adds one series inductor
    per matched net between that net's star/Gamma-shunt hub and its ground
    capacitor (``hub --L--> <fresh node> --C--> ground``, in henries in the
    written SPICE ``L`` card) -- there is no inductance term anywhere in
    this module's default quasi-static RC-only model
    (``PARASITIC_MODEL_SCOPE``) for this to replace, so it is purely
    additive rather than a substitution. ``None`` (the default) for all four
    parameters skips this feature entirely -- byte-identical to before this
    feature existed. The applied override (and the pre-substitution lumped
    value it replaced) is reported in the new
    ``parasitics.mom_rlc_override`` block -- see
    ``docs/cli/extract.md``'s ``--mom-rlc-net`` section for the full field
    list.

    ``matched_device_groups`` (``klt extract --matched-group
    NAME=INST1,INST2[,...]``, repeatable, issue #1018) declares a set of
    device instances (``devices[].name``, e.g. ``"$1"``) that are expected to
    stay geometrically matched -- a differential pair, or a current-mirror
    leg -- and checks, after extraction, that every parameter every member
    reports in common (``devices[].params``, e.g. ``w_um``/``l_um`` for a MOS
    pair, ``r_ohm`` for a matched resistor pair) is identical across the
    whole group. This is a **self-consistency check within one extracted
    netlist**, not a comparison against a reference netlist (that is ``klt
    lvs``'s job, via ``options.parameter_tolerance``) -- it catches a
    hand-edit slip or a mis-parameterized generator call that silently broke
    a matching assumption the sizing exercise was based on. Values are
    compared post-rounding (``_PARAM_PRECISION_UM``/``_PARAM_PRECISION_OHM``
    already clear floating-point noise, so no separate numeric-tolerance
    concept is needed here). A group name repeated across two
    ``--matched-group`` flags, or fewer than two instance names in one
    ``NAME=...`` entry, is an :class:`ExtractError` -- a likely typo, not a
    meaningful "declare a group of one" request, matching
    ``--deck-option``'s own "a flag naming something malformed is an error"
    convention. An instance name that matches no extracted device in this
    layout is *not* an error -- it is reported per-group in
    ``matched_device_groups[].unresolved_instances`` and a matching
    ``warnings`` entry, mirroring ``--critical-net``'s tolerant "declared but
    absent" convention, since a caller may legitimately reuse a group
    declaration across several layout variants. ``None``/empty (the default)
    skips this entirely -- byte-identical to before this feature existed. See
    :func:`_describe_matched_device_groups` for the exact comparison and
    ``docs/cli/extract.md``'s "Matched-device geometry check" section for a
    worked example.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/extract.md`` / ``docs/design/lvs-extraction-spike.md``
    section 2a)::

        {
            "schema_version": 1,
            "file": <path as provided>,
            "deck": <deck name>,
            "top": <top cell name>,
            "dbu_um": <database unit in micrometres, float>,
            "netlist_path": <resolved output path>,
            "netlist_sha256": <hex digest of the written netlist file>,
            "status": "extracted",
            "device_count": <int>,
            "net_count": <int>,
            "pin_count": <int>,
            "device_counts": {<device class>: <int>, ...},
            "ignored_layers": [
                {"layer": int, "datatype": int, "shapes": int}, ...
            ],
            "device_recognition_only_layers": [
                {"layer": int, "datatype": int, "shapes": int}, ...
            ],
            "device_classes": [<device class role>, ...],
            "devices": [
                {
                    "name": str, "class": str,
                    # MOS: {"s", "g", "d", "b"}; drawn resistor: {"a", "b"}
                    # (plus "w" for a bulk-terminal resistor).
                    "nets": {<terminal>: str | None, ...},
                    # MOS: {"w_um", "l_um"}; drawn resistor adds "r_ohm".
                    "params": {<name>: float, ...},
                },
                ...
            ],
            "nets": [{"name": str, "pin": bool, "device_count": int}, ...],
            "warnings": [str, ...],
            "black_box_regions": [
                {
                    "bbox_um": {
                        "left": float, "bottom": float,
                        "right": float, "top": float,
                    },
                    "shapes_excluded": int,
                },
                ...
            ],
            "abstracted_cells": [
                {
                    "cell": str, "instance_count": int, "pin_count": int,
                    "resolution_source": "in_cell_labels" | "lef_abstract",
                    "lef_path": str | None,
                },
                ...
            ],
            "unmodelled_poly": [
                {
                    "bbox_um": {
                        "left": float, "bottom": float,
                        "right": float, "top": float,
                    },
                    "reason": "unmarked" | "marked_unrecognised",
                },
                ...
            ],
            "voltage_domain_warnings": [
                {"marker": "<layer>/<datatype>", "description": str}, ...
            ],
            "merged_net_labels": [
                {"net": str, "labels": [str, ...]}, ...
            ],
            "unbiased_pmos_body_nets": [
                {"device": str, "net": str}, ...
            ],
            "single_terminal_nets": [
                {
                    "net": str, "device": str, "terminal": str,
                    "terminal_kind": str,
                },
                ...
            ],
            "dead_metal": [
                {
                    "role": str, "layer": int, "datatype": int,
                    "bbox_um": {
                        "left": float, "bottom": float,
                        "right": float, "top": float,
                    },
                    "shapes": int, "area_um2": float,
                },
                ...
            ],
            "matched_device_groups": [
                {
                    "name": str, "instances": [str, ...],
                    "unresolved_instances": [str, ...],
                    "mismatched_fields": [
                        {"field": str, "values": {<instance name>: float, ...}},
                        ...
                    ],
                },
                ...
            ],
            "pdk": {
                "variant": str,
                # {path, scope} (issue #1376), never a raw path -- see
                # `env_provenance.repo_relative_path`. `scope` is one of
                # "repo" / "external" / "absent"; `path` is `null` unless
                # `scope == "repo"`. A PDK install is virtually always
                # "external" in practice.
                "root": {"path": str | None, "scope": str},
                "version": str | None,
            } | None,
            "parasitics": {...} | None,
            "spef_path": <str | None>,  # populated only when `spef_output` was given
            "provenance": {  # shared reproducibility block, see _provenance.py
                "klt_version": <str | None>,
                "klayout_version": <str | None>,
                "pdk": {"name", "source", "version"} | None,
                # "options" is present only when `deck_options` was given
                # (issue #595) -- omitted entirely otherwise.
                "deck": {
                    "name": <deck name>,
                    "content_hash": "sha256:...",
                    "options": {<deck option key>: <value>, ...},
                },
            },
        }

    ``devices``/``nets`` are sorted by name for deterministic, diff-clean
    output (same discipline as ``drc.py``'s ``violations`` sort).

    ``device_classes`` is what the *deck* is structurally capable of
    recognising (:attr:`klayout_tools.decks.ExtractionDeck.device_classes`)
    -- independent of what this particular layout happens to contain, unlike
    ``device_counts`` (issue #221). A consumer that needs to know ahead of
    time whether a deck can even produce a given device class (e.g. before
    pairing it with a reference netlist for ``klt lvs``) reads this field
    instead of inferring "not supported" from a zero count.

    ``ignored_layers`` (issue #220) lists ``(layer, datatype)`` pairs that
    carry shapes in the input stream but are *not* read by this deck's
    connectivity graph (:attr:`ExtractionDeck.connectivity_layers`), each with
    its stream shape count. It is the extraction-side analogue of ``klt
    drc``'s ``coverage.layers_in_stream_without_rules``: geometry on such a
    layer is invisible to extraction, so a block routed on a metal level the
    deck does not declare silently extracts as a pile of disconnected nets.
    A non-empty ``ignored_layers`` with a material shape count is the signal
    that a downstream ``klt lvs`` mismatch is a deck-coverage gap, not a
    layout bug. Empty when every shape-bearing layer is one the deck reads.
    Every entry already carries a material (``shapes > 0``) count -- empty
    layers are dropped before they reach this field -- so a non-empty
    ``ignored_layers`` also appends a single aggregate prose entry to
    ``warnings[]`` (issue #666), naming the affected layers and their total
    shape count, so a caller checking only ``warnings[]`` still sees it.

    ``device_recognition_only_layers`` (issue #619) lists ``(layer,
    datatype)`` pairs that carry shapes in the input stream *and are read* by
    this deck (so they never appear in ``ignored_layers`` above) but only for
    a ``bipolars``/``capacitors``/``resistors``/``diodes`` device-recognition
    role -- never as a ``metals``/``vias`` connectivity level, and never one
    of the deck's own MOS-core layers either
    (:attr:`ExtractionDeck.device_recognition_only_layers`). Two nets joined
    only through such a layer will not merge, and this gap is invisible to
    ``ignored_layers`` because the layer genuinely is read, just not for
    net-merging purposes -- exactly how sky130's own met3/met4 (its MiM-cap
    bottom plates) hid a routing-connectivity ceiling behind a clean-looking
    ``ignored_layers`` report before its ``metals`` stack grew to cover them
    too. This is diagnostic context, not a warning: unlike ``ignored_layers``,
    a non-empty list does *not* append to ``warnings[]`` -- a deck's own
    marker/mask geometry (a resistor's marker layer, a bipolar's ID mark, a
    MiM cap's top-plate mark) is expected to be device-recognition-only by
    PDK design, not a coverage gap, so flagging every occurrence would make
    ``warnings[]`` fire on nearly every layout that uses one of these device
    classes. Empty when every device-recognition layer is also a
    ``metals``/``vias`` connectivity level or one of the deck's own MOS-core
    layers (or the deck declares no ``bipolars``/``capacitors``/
    ``resistors``/``diodes`` entries at all).

    ``black_box_regions`` (issue #293) reports every black-box/abstract
    region this run excluded from connectivity: a shape drawn on any
    reserved annotation layer (990-999, any datatype -- issue #289, see
    ``docs/cli/extract.md``'s "Reserved annotation layer") marks a region
    whose contents are deliberately out of scope -- a sub-cell that will be
    drawn later, or a drawn region deliberately out of scope for a compare.
    Everything geometrically inside it is excluded from the connectivity
    graph *before* device extraction runs, rather than left undrawn (losing
    the hierarchy/area record) or documented only in prose outside the GDS.
    One entry per geometrically separate marker shape (non-touching shapes
    are never merged into one bbox), each ``{"bbox_um": {"left", "bottom",
    "right", "top"}, "shapes_excluded": <int>}`` -- ``shapes_excluded`` counts
    the conductor/label shapes actually removed by the exclusion, the signal
    that it did something rather than that a marker shape merely exists.
    Always a list, empty when the layout draws no reserved-layer geometry
    (byte-identical to the response before this field existed, other than
    the field's own presence).

    ``abstract_cell_patterns``/``abstract_cell_lef_paths`` (``klt extract
    --abstract-cells '<glob>'``/``--abstract-cell-lef <path>``, both
    repeatable; issue #620) select a **cell-level black-box** abstraction
    mode, additive to (and independent of) ``black_box_regions`` above: every
    instantiated cell whose name matches one of the ``fnmatch`` glob patterns
    in ``abstract_cell_patterns`` is extracted as an opaque, pinned
    subcircuit instead of being flattened down to its own devices --
    everything *not* matched by a pattern is extracted exactly as it is
    today. A matched cell's pins are resolved, per distinct cell *type*
    (cached across every occurrence): first from that cell's own
    ``metal_labels``/``well_label``/``poly_label`` text, drawn directly in
    its own definition (never promoted from a nested sub-cell); when a
    matched type draws no such label, from a ``MACRO``/``PIN``/``PORT``
    block of the same name in one of the ``abstract_cell_lef_paths`` LEF
    files/directories, in the order given. A matched cell type resolved by
    neither source is an :class:`ExtractError` naming it (a caller must
    either supply a pin source or narrow the pattern) -- see
    :func:`_wire_abstract_cells`. ``abstract_cell_patterns`` empty (the
    default) skips this mode entirely; the written SPICE and every other
    field are then byte-identical to before this feature existed.
    ``abstract_cell_lef_paths`` is only ever consulted as the fallback pin
    source and has no effect when every matched cell type resolves its pins
    from in-cell labels.

    ``abstracted_cells`` reports this mode's own result: one entry per
    *distinct* matched cell type (sorted by cell name), each
    ``{"cell": <cell type name>, "instance_count": <int>, "pin_count":
    <int>, "resolution_source": "in_cell_labels" | "lef_abstract",
    "lef_path": <str | None>}`` -- ``lef_path`` names the specific LEF file
    the pins were resolved from, ``None`` for ``"in_cell_labels"``. Always a
    list, empty when ``abstract_cell_patterns`` is empty or matches no
    instantiated cell.

    The written SPICE gains one ``.SUBCKT <cell type> <pins...> ... .ENDS``
    block per distinct matched cell type (empty body -- a black box declares
    no devices) and one ``X<instance>`` card per matched instance in the top
    circuit's own ``.SUBCKT`` block, wired to the same layout-derived net
    names the un-abstracted portion of the circuit already uses -- KLayout's
    native ``kdb.SubCircuit``/``NetlistSpiceWriter`` machinery emits this
    automatically once the netlist model represents the abstraction (every
    circuit, including the flat top-level one, is already written as its own
    ``.SUBCKT`` block today, so this is a purely additive extension of the
    same writer, not a new SPICE-emission code path). See
    ``docs/cli/extract.md``'s "Cell-level (black-box + pins) abstraction"
    section for worked examples.

    ``unmodelled_poly`` (issue #324) reports every poly shape the
    "unmodelled device" diagnostic flagged -- see
    :func:`_detect_unmodelled_poly_bodies` for the exact signature it looks
    for (a poly component touching no recognised MOS gate or recognised
    resistor body, contacted at 2+ geometrically separate points) and
    ``docs/cli/extract.md``'s "Known limitation: unmodelled device geometry"
    for the heuristic's documented false-negative/false-positive surface.
    One entry per flagged component, each ``{"bbox_um": {"left", "bottom",
    "right", "top"}, "reason": "unmarked" | "marked_unrecognised"}`` --
    ``reason`` distinguishes the two ``warnings`` cases (#288's "no marker at
    all" versus #299's "carries a marker but no declared entry claims it")
    without a consumer having to parse the prose warning string. Sorted by
    ``(left, bottom)`` for deterministic, diff-clean output. Always a list,
    empty whenever ``warnings`` carries no unmodelled-device entry (which
    includes every layout that draws no such geometry at all) -- a consumer
    can enumerate and triage the exact flagged shapes instead of
    re-implementing the heuristic against the stream.

    ``voltage_domain_warnings`` (issue #552) reports every voltage-domain
    marker layer (registered per-deck via
    :func:`~klayout_tools.decks.get_unmodeled_voltage_markers`, e.g.
    gf180mcu's ``Dualgate`` 55/0) whose geometry overlaps extracted MOS
    device geometry -- see :func:`_detect_voltage_domain_overlap`. A marker
    the deck's ``ExtractionDeck.mos_flavours`` also declares (issue #1111,
    gf180mcu's ``Dualgate`` as of that issue) is excluded here: MOS
    recognition for that marker *is* now flavour-aware (a transistor drawn
    inside it extracts bound to the flavour's own real model, e.g.
    ``nfet_06v0``/``pfet_06v0``, under ``--pdk``), so the gap this warning
    exists to flag no longer applies to it -- only a marker with no
    ``mos_flavours`` coverage (deriving MOS flavour from the well layer
    alone, still binding every transistor to the deck's single default
    model regardless of the marker) is flagged. One entry per flagged
    marker, each ``{"marker": "<layer>/<datatype>", "description": str}`` --
    the same registry entry ``klt drc``'s ``coverage.voltage_domain_warnings``
    surfaces for the same deck (that command's own per-*rule* gate is
    independent of this ``mos_flavours`` exclusion -- see
    ``decks/gf180mcu.py``'s ``UNMODELED_VOLTAGE_MARKERS`` note), so the
    wording matches across both commands wherever both still flag the same
    marker. A matching prose entry is also appended to ``warnings``. Always
    a list, empty for a deck that registers no such marker, a marker fully
    covered by ``mos_flavours``, or a layout that draws none of it
    overlapping MOS geometry.

    ``merged_net_labels`` (issue #470) reports every net whose KLayout-
    assigned name is a comma-joined merge of 2+ distinct labels -- see
    :func:`_detect_merged_net_labels` for the exact heuristic and
    ``docs/cli/extract.md``'s "Merged net labels" section for the false-
    positive limitation. One entry per affected net, each ``{"net": "<full
    joined name>", "labels": [str, ...]}`` -- ``labels`` is the joined name
    split on ``,``, so a consumer does not have to re-derive the label list
    from the string itself. A matching prose entry is also appended to
    ``warnings`` for every affected net. Always a list, empty when no net
    carries multiple labels.

    ``unbiased_pmos_body_nets`` (issue #555) reports every extracted PMOS
    (``deck.pfet_class``) device whose body (``"b"``) terminal ties to an
    anonymous, KLayout-synthesized net -- the ``"$5"``-style placeholder
    ``Net.expanded_name()`` assigns to a net with no drawn label, as opposed
    to the deck's own synthesized (but *named*) global substrate net (e.g.
    ``"vsubs"``, tied via ``connect_global``). This happens whenever no well
    tie -- drawn on a distinct ``tap`` layer, or derived from
    ``tap_nplus``/``tap_pplus`` implants (issue #1084) -- reaches a given
    PMOS device's ``nwell`` island; a deck with neither mechanism declared
    at all hits this for *every* PMOS unconditionally (gf180mcu, before
    #1084 gave it a derivable ``tap_nplus``/``tap_pplus`` pair -- see
    ``decks/gf180mcu.py``), while a deck that declares one but whose
    specific layout draws no tie still hits it per-device. Unlike
    ``devices[].nets["b"]`` (which already carries the same net name, just
    not flagged), this field is the structured, no-grep-required signal that
    the reported net has **no DC bias path at all** -- see
    ``docs/cli/extract.md``'s "Parasitic (RC) extraction" section for the
    simulation-fidelity consequence (a floating body voltage rather than the
    real supply rail every schematic-level netlist assumes). One entry per
    affected device, each ``{"device": "<device instance name>", "net":
    "<anonymous net name>"}``; a single aggregate prose entry (count baked
    in, e.g. ``"148 PMOS devices tie their body to an anonymous net..."``)
    is also appended to ``warnings`` when this field is non-empty -- not one
    line per device (issue #599), so ``warnings`` does not scale with the
    device count on a large design. Always a list, empty when no PMOS
    device's body net is anonymous (which includes every deck whose layer
    set draws a real well-tie/tap). Independent of ``--parasitics``/
    ``--pdk`` -- present under the same condition regardless of either flag,
    since the DC-bias gap exists whether or not parasitics are requested or
    a PDK model is bound.

    ``single_terminal_nets`` (issue #596) reports every net whose
    ``nets[]`` entry has ``device_count == 1`` (``Net.terminal_count()``,
    already reported per net -- see the ``nets`` field above) and
    ``pin: False`` -- a net that touches exactly one device terminal and is
    not a declared top-level pin. There is no DC path through such a node
    from anywhere else in the netlist, so a downstream simulator hits a
    singular matrix on it; this is the structurally-detectable signal for
    that failure, several stages upstream of where a transient solver would
    otherwise report it against an anonymous net name. See
    :func:`_detect_single_terminal_nets` for the exact detection and
    ``terminal_kind`` classification. One entry per affected net, each
    ``{"net": "<net name>", "device": "<owning device instance name>",
    "terminal": "<lower-cased terminal key>", "terminal_kind": "gate" |
    "source" | "drain" | "body" | "<literal terminal key>"}`` --
    ``terminal_kind`` is the MOS terminal name for a MOS-like device (a
    device with a ``"g"`` terminal), else the raw terminal key itself (the
    "resistor-equivalent" case, e.g. a drawn resistor/capacitor's ``"a"``/
    ``"b"``/``"w"``). One aggregate prose entry per ``terminal_kind`` bucket
    (count baked in) is also appended to ``warnings`` when that bucket is
    non-empty -- not one line per net (issue #599), so ``warnings`` does not
    scale with the affected net count on a large design. Up to two such
    entries: one for ``terminal_kind == "gate"``, phrased more strongly (an
    undriven MOS input is essentially always a bug), and one for every other
    terminal kind combined (a single-terminal source/drain/body/resistor-
    style tie can be a legitimate deliberately-unterminated dummy). Always a
    list, empty when every net either has zero or 2+ device terminals, or is
    a declared pin.

    ``dead_metal`` (issue #676) reports every connected cluster of
    routing-stack geometry -- the deck's ``metals``/``vias`` levels -- that
    joins **no** extracted net: nothing in ``nets[]`` mentions it, so it is
    invisible to this report, to ``klt lvs``, and to a resimulation of the
    written netlist. One entry per cluster (not per drawn polygon), each
    ``{"role": "metal<i>" | "via<i>", "layer": int, "datatype": int,
    "bbox_um": {"left", "bottom", "right", "top"}, "shapes": int,
    "area_um2": float}``, sorted by ``(layer, datatype, left, bottom)`` --
    ``role``'s ``<i>`` indexes the deck's own ``metals``/``vias`` tuple
    (``0`` = the bottom-most level), and ``shapes`` counts the drawn shapes
    on that stream layer the cluster covers, so a reviewer knows how much
    geometry to go look at. The netted side of the subtraction comes from the
    extracted connectivity graph, so **XY overlap between adjacent metal
    levels is not connection**: a wire passing over another with no via
    between them stays dead. A non-empty list also appends a single aggregate
    prose entry to ``warnings[]`` (count baked in, issue #599's pattern) --
    dead metal is often deliberate (artwork, fill, a bond-pad blank), which is
    exactly why a reviewer should be told it is there rather than left to
    discover it by rendering the raw geometry. A *labelled* floating cluster
    is not dead: :func:`_purge_preserving_named_nets` keeps it as a real,
    named, pinned net, so power straps/seal rings/bond pads that carry a
    label never appear here. See :func:`_detect_dead_metal` and
    ``docs/cli/extract.md``'s "Dead metal" section. Always a list, empty when
    every metal/via shape joins a net.

    ``matched_device_groups`` (issue #1018) is one entry per ``--matched-
    group`` declaration, in the order given -- see ``matched_device_groups``
    above ``run_extract``'s own parameter docstring for the full contract.
    Each entry is ``{"name": <group name>, "instances": [<instance name>,
    ...], "unresolved_instances": [<instance name>, ...],
    "mismatched_fields": [{"field": <param name>, "values": {<instance
    name>: <float>, ...}}, ...]}`` -- ``instances`` echoes the declared
    member list verbatim (as given, not sorted/deduplicated, matching
    ``parasitics.critical_nets``'s own echo convention);
    ``unresolved_instances`` (sorted) is the subset that matched no extracted
    device; ``mismatched_fields`` is empty when every parameter every
    *resolved* member reports in common agrees across the whole group (which
    includes the case of fewer than two resolved members -- nothing to
    compare). See :func:`_describe_matched_device_groups` for the exact
    comparison. Always a list, empty when ``matched_device_groups`` (the
    ``--matched-group`` flag) was never given.

    ``parasitics.metals_without_coefficient`` (issue #547) lists every metal
    stack level the deck's ``ExtractionDeck.metals`` declares that has no
    matching entry in the deck's ``ParasiticsDeck.metals`` -- the
    extraction-side analogue of ``ignored_layers``, but for the parasitics
    pass's *own* coefficient table rather than the input stream. A metal
    level in this list silently contributes zero resistance and capacitance
    to every net's parasitics; see :func:`_describe_parasitics_metal_gaps`
    for the exact gap definition. Present only inside the ``parasitics``
    block (so only when ``--parasitics`` was given); a matching prose entry
    is also appended to ``warnings`` when the list is non-empty. Always a
    list, empty when every declared metal level has a coefficient.

    ``parasitics.overlap_pairs_without_coefficient`` (issue #760) is the same
    gap report for the vertical-overlap *coupling* coefficient family: one
    entry per adjacent metal-level pair the deck declares (``metals[i]``/
    ``metals[i+1]``) with no matching entry in
    ``ParasiticsDeck.metal_overlaps``. That pair's area still charges to
    ground in full -- as if this feature did not exist -- rather than moving
    to a coupling capacitor; see
    :func:`_describe_parasitics_overlap_gaps` for the exact gap definition.
    Present only inside the ``parasitics`` block; a matching prose entry is
    also appended to ``warnings`` when non-empty. Always a list, empty when
    every declared adjacent metal-level pair has a coupling coefficient
    (true for both shipped decks today).

    Raises :class:`ExtractError` if the file is missing/unreadable, the deck
    name is unknown, ``deck_options`` names an unrecognised key/value, the
    PDK (when given) does not resolve, the top cell is missing/ambiguous, an
    ``abstract_cell_lef_paths`` entry cannot be read, a matched
    ``abstract_cell_patterns`` cell type resolves no pins from either source,
    or the output path's parent directory cannot be created (e.g. it exists
    as a non-directory file). The output path's parent directory is created
    automatically when missing (matching ``klt render``/``klt lvs``),
    including any missing intermediate directories.
    """
    if abstract_cell_lef_paths and not abstract_cell_patterns:
        raise ExtractError(
            "abstract_cell_lef_paths (--abstract-cell-lef) was given but "
            "abstract_cell_patterns (--abstract-cells) is empty -- "
            "--abstract-cell-lef only has an effect as a pin-resolution "
            "fallback for a cell type --abstract-cells actually matches"
        )
    pdk_info: dict[str, Any] | None = None
    # Populated only when a PDK resolves: `{<deck's device class name>:
    # DeviceBinding}` for every device class this deck extracts that has a
    # curated binding (MOS + resistor + capacitor on both decks, plus sky130's
    # bipolar; gf180mcu's bipolar is a documented carve-out and stays absent --
    # see `klayout_tools.pdk_models`). Drives the `X`-card model-binding writer
    # below -- see the module docstring's "--pdk-triggered model binding" note.
    model_bindings: dict[str, DeviceBinding] | None = None
    if pdk_variant is not None or pdk_root is not None:
        try:
            pdk_info = find_pdk(variant=pdk_variant, root=pdk_root)
        except PdkNotFoundError as exc:
            raise ExtractError(str(exc)) from exc

        try:
            deck_for_models = get_extraction_deck(deck_name, deck_options)
        except (UnknownExtractionDeckError, InvalidDeckOptionError) as exc:
            raise ExtractError(str(exc)) from exc
        try:
            model_bindings = resolve_device_bindings(
                deck_name, pdk_info["variant"], deck_for_models
            )
        except ModelBindingError as exc:
            raise ExtractError(str(exc)) from exc

    # `--parasitics` resolves the curated per-PDK RC coefficient table for
    # this deck (see `klayout_tools.decks.ParasiticsDeck`); when the flag is
    # omitted the whole parasitics path is skipped and the written SPICE /
    # JSON are byte-identical to before this feature existed (additive, off
    # by default -- see docs/cli/extract.md and issue #216's addendum).
    parasitics_deck: ParasiticsDeck | None = None
    if parasitics:
        try:
            parasitics_deck = get_parasitics_deck(deck_name)
        except UnknownExtractionDeckError as exc:
            raise ExtractError(str(exc)) from exc

    # `--mom-net` (issue #798) requires `--parasitics`: it cross-checks (and
    # replaces) one net's lumped-RC ground capacitance, so there is nothing
    # to cross-check against without the lumped-RC pass this flag piggybacks
    # on. An explicit error here, before the (potentially expensive) real
    # extraction runs, matches this module's existing "a flag naming
    # something invalid is an error, not a silent no-op" convention (e.g.
    # `--abstract-cell-lef` without `--abstract-cells` above).
    if mom_net is not None and not parasitics:
        raise ExtractError("--mom-net requires --parasitics")

    # `--spef` (issue #948) requires `--parasitics`, same reasoning as
    # `--mom-net` above: there is no per-net R/C model to translate into SPEF
    # without the lumped-RC pass this flag reuses (see :func:`_write_spef`).
    if spef_output is not None and not parasitics:
        raise ExtractError("--spef requires --parasitics")

    # `--critical-net` (issue #976) requires `--parasitics`: it scopes the
    # lateral-coupling pass onto the same lumped-RC extraction this flag
    # piggybacks on, same reasoning as `--mom-net`/`--spef` above.
    critical_nets_set = frozenset(critical_nets) if critical_nets else None
    if critical_nets_set is not None and not parasitics:
        raise ExtractError("--critical-net requires --parasitics")

    # `--distributed-rc` (issue #977) requires `--critical-net`: it reuses
    # that flag's own net set as the "which nets get the ladder" scope
    # rather than inventing a second net classification mechanism -- with
    # nothing named there is nothing to scope this onto.
    if distributed_rc and not critical_nets_set:
        raise ExtractError("--distributed-rc requires --critical-net")

    # `--mom-rlc-net` (issue #988, Epic #709 Phase 3a): substitutes a
    # caller-supplied R/L/C for one named net's Phase 1/2 lumped-RC model --
    # see `run_extract`'s own `mom_rlc_net` docstring paragraph. Validated up
    # front, same "a flag naming something invalid is an error, not a silent
    # no-op" convention every other opt-in flag above follows.
    mom_rlc_values_given = (
        mom_rlc_resistance_ohm is not None
        or mom_rlc_capacitance_ff is not None
        or mom_rlc_inductance_nh is not None
    )
    if mom_rlc_net is not None and not mom_rlc_values_given:
        raise ExtractError(
            "--mom-rlc-net requires at least one of --mom-rlc-resistance-ohm/"
            "--mom-rlc-capacitance-ff/--mom-rlc-inductance-nh"
        )
    if mom_rlc_net is None and mom_rlc_values_given:
        raise ExtractError(
            "--mom-rlc-resistance-ohm/--mom-rlc-capacitance-ff/"
            "--mom-rlc-inductance-nh require --mom-rlc-net"
        )
    if mom_rlc_net is not None and not parasitics:
        raise ExtractError("--mom-rlc-net requires --parasitics")
    for label, value in (
        ("--mom-rlc-resistance-ohm", mom_rlc_resistance_ohm),
        ("--mom-rlc-capacitance-ff", mom_rlc_capacitance_ff),
        ("--mom-rlc-inductance-nh", mom_rlc_inductance_nh),
    ):
        if value is not None and value < 0:
            raise ExtractError(f"{label} must be >= 0 (got {value!r})")
    if (
        mom_rlc_net is not None
        and distributed_rc
        and critical_nets_set is not None
        and mom_rlc_net in critical_nets_set
    ):
        raise ExtractError(
            f"--mom-rlc-net {mom_rlc_net!r} also names a --distributed-rc "
            "net -- a caller-supplied lumped R/L/C override and a "
            "multi-segment distributed ladder cannot both model the same "
            "net's parasitics at once"
        )

    # `--matched-group` (issue #1018): a declared group needs at least two
    # instance names -- there is nothing to compare with just one, so this is
    # a likely typo rather than a meaningful "declare a group of one"
    # request, matching this module's existing "a flag naming something
    # malformed is an error, not a silent no-op" convention (e.g.
    # `--deck-option`'s own KEY=VALUE validation).
    if matched_device_groups:
        for group_name, instance_names in matched_device_groups.items():
            if len(instance_names) < 2:
                raise ExtractError(
                    f"--matched-group {group_name!r} names "
                    f"{len(instance_names)} instance(s) -- a matched group "
                    "needs at least two instances to compare"
                )

    (
        netlist,
        top_cell_name,
        dbu_um,
        warnings,
        parasitic_nets,
        black_box_regions,
        dummy_devices_dropped,
        unmodelled_poly,
        voltage_domain_warnings,
        abstracted_cells,
        dead_metal,
        mom_crosscheck,
    ) = extract_netlist_from_layout(
        path,
        deck_name,
        top=top,
        parasitics_deck=parasitics_deck,
        top_cell_pins_only=top_cell_pins_only,
        declared_pins=declared_pins,
        apply_resistor_fixed_offset=apply_resistor_fixed_offset,
        deck_options=deck_options,
        abstract_cell_patterns=abstract_cell_patterns,
        abstract_cell_lef_paths=abstract_cell_lef_paths,
        mom_net=mom_net,
        mom_background_permittivity=mom_background_permittivity,
        def_net_names=def_net_names,
        critical_nets=critical_nets_set,
        def_pins=def_pins,
    )

    if mom_net is not None:
        if mom_crosscheck is None:
            raise ExtractError(f"--mom-net {mom_net!r} matches no net in this layout")
        if mom_crosscheck["mom_capacitance_ff"] is None:
            reason = "; ".join(mom_crosscheck["warnings"]) or "no reason given"
            raise ExtractError(f"--mom-net {mom_net!r}: {reason}")

    import klayout.db as kdb

    netlist_path = output if output is not None else _default_output_path(path)
    out_dir = os.path.dirname(os.path.abspath(netlist_path))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        raise ExtractError(f"cannot create output directory {out_dir}: {exc}") from exc

    # `_purge_preserving_named_nets()` (in `_extract_netlist`) still drops a
    # circuit entirely when it has no devices, no named/labelled nets, and no
    # subcircuits -- e.g. a layout with no extractable devices and no named
    # nets. That is a legitimate "nothing extracted" result, not an error:
    # report zero devices/nets rather than dereferencing a `None` circuit. A
    # circuit with named/labelled nets but zero devices survives with those
    # nets/pins intact instead (issue #539) -- `circuit` below is only ever
    # `None` for the genuinely-empty case.
    #
    # `devices[]`/`nets[]` are built from the schematic-equivalent netlist
    # *before* any parasitic R/C is injected, so they carry their exact
    # documented meaning whether or not `--parasitics` was given (the
    # additive-contract requirement from issue #216's addendum): parasitic
    # elements never appear in `device_count`/`devices[]`, and the internal
    # parasitic nodes never appear in `net_count`/`nets[]` -- they live only
    # in the written SPICE and in the separate `parasitics` block below.
    # Already validated by `extract_netlist_from_layout` above (it would have
    # raised `ExtractError` on an unknown deck or an invalid `deck_options`
    # entry before reaching this point), so re-fetching it here (with the
    # same `deck_options`, so a selected resistor flavour's name is reflected
    # consistently) to read its static device-class coverage
    # (`device_classes`, issue #221) and its `substrate_net` cannot itself
    # raise.
    deck = get_extraction_deck(deck_name, deck_options)

    circuit = netlist.circuit_by_name(top_cell_name)
    if circuit is not None:
        # The deck's two-term device-parameter corrections
        # (`CapacitorDevice.perim_cap_f_um`, issue #512;
        # `ResistorDevice.fixed_offset_ohm`, issue #518) were already applied
        # to the `kdb.Device` objects by `_apply_device_parameter_corrections`
        # inside `_extract_netlist` (issue #521), so `_describe_devices` just
        # reads them back -- and the `netlist.write(...)` below therefore
        # writes the *same* corrected values into the SPICE file that
        # `devices[].params` reports.
        devices, device_counts = _describe_devices(circuit)
        nets = _describe_nets(circuit)
    else:
        devices, device_counts, nets = [], {}, []

    # `--matched-group` (issue #1018): a caller-declared set of device
    # instances expected to stay geometrically matched (a differential pair,
    # a current-mirror leg) -- see `run_extract`'s own `matched_device_groups`
    # docstring paragraph and `_describe_matched_device_groups`'s docstring
    # for the exact comparison. Computed from the already-built `devices[]`
    # (so it reuses the same rounded `params` values every other consumer of
    # this response reads), independent of `--parasitics`/`--pdk`.
    matched_device_groups_report, matched_group_warnings = (
        _describe_matched_device_groups(matched_device_groups, devices)
    )
    warnings.extend(matched_group_warnings)

    # Nets whose KLayout-assigned name is a multi-label merge (issue #470):
    # `Net.expanded_name()` joins every distinct label found on one
    # electrical net with `,` (see `tests/test_extract.py`'s
    # `_make_inverter_layout(extra_y_label=...)` fixture, built for issue
    # #312's SPICE-name-sanitization fix), but this response's `nets[]`
    # (built by `_describe_nets`, above) has already rewritten that to the
    # `|`-joined spelling KLayout's own `NetlistSpiceWriter` uses for the
    # net's node references in the written SPICE (`spice_safe_net_name`,
    # issue #696) -- so `merged_net_labels[].net` below is byte-identical to
    # the netlist's own spelling, not a separately (comma-) spelled alias of
    # it. That is a silent signal that two differently-named nets were
    # shorted together on the layout side -- e.g. a `gen-compose` `pins[]`
    # entry naming a port that other connectivity already reaches. Surfaced
    # two ways: a structured `merged_net_labels[]` entry (so a caller does
    # not have to re-derive the label list from the joined string) and a
    # matching prose `warnings[]` entry (so a caller checking only
    # `warnings[]`, per the documented contract, still sees it). See
    # docs/cli/extract.md's "Merged net labels" section for the
    # false-positive limitation: a label that legitimately contains a
    # literal `|` is indistinguishable from a real collision by this
    # heuristic.
    merged_net_labels = _detect_merged_net_labels(nets)
    for merged_entry in merged_net_labels:
        labels_str = ", ".join(merged_entry["labels"])
        warnings.append(
            f"net '{merged_entry['net']}' merges "
            f"{len(merged_entry['labels'])} distinct labels ({labels_str}) "
            "onto one net -- KLayout joins multiple labels found on the "
            "same electrical net (as '|' here and in the written netlist, "
            "matching NetlistSpiceWriter's own node-name escaping); this "
            "usually means two differently-named nets were shorted "
            "together in the layout -- see docs/cli/extract.md's "
            "'Merged net labels' section."
        )

    # PMOS body terminals tied to an anonymous, unbiased net (issue #555):
    # a PMOS body extracts onto a KLayout-synthesized `$<n>` net with no DC
    # bias path whenever no well tie -- drawn on a distinct `tap` layer, or
    # derived from `tap_nplus`/`tap_pplus` implants (issue #1084) -- reaches
    # this specific device's `nwell` island; a deck with neither mechanism
    # at all (e.g. gf180mcu before #1084) hits this for *every* PMOS,
    # unconditionally. The net name was already readable via
    # `devices[].nets["b"]`, but nothing flagged it as *this specific* gap.
    # Surfaced two ways: a structured `unbiased_pmos_body_nets[]` entry (so
    # a caller does not have to re-derive the anonymous-net convention
    # itself) and a matching prose `warnings[]` entry. The `warnings[]`
    # entry is a single aggregate line with the count baked in, mirroring
    # `_detect_unmodelled_poly_bodies`'s aggregate pattern (issue #599) --
    # one line per device would blow up `warnings[]` at scale (e.g. 148
    # entries for 148 floating PMOS bodies) and defeat literal-list pinning
    # by a caller.
    unbiased_pmos_body_nets = _detect_unbiased_pmos_body_nets(devices, deck)
    if unbiased_pmos_body_nets:
        device_word = "device" if len(unbiased_pmos_body_nets) == 1 else "devices"
        warnings.append(
            f"{len(unbiased_pmos_body_nets)} PMOS {device_word} tie their "
            "body to an anonymous net with no DC bias path -- no drawn (or "
            f"derivable) well tie connects this PMOS body to a real supply "
            "rail on this layout, so it is left floating rather than tied "
            "to a real supply rail; resimulating this netlist directly "
            "will not reproduce the schematic-level PMOS body bias -- see "
            "unbiased_pmos_body_nets[] for the full list. See "
            "docs/cli/extract.md's 'Parasitic (RC) extraction' section."
        )

    # `--pdk`-bound device classes whose target subcircuit has no parameter
    # for something the extractor measured (issue #695): today, only a
    # sky130 `pnp` bipolar binding, whose fixed-geometry `pnp_05v5_*` cells
    # take no per-instance base/collector-area/perimeter or emitter-count
    # override at all (see `pdk_models.DeviceBinding.dropped_params` and
    # `_BIPOLAR_DROPPED_PARAMS`'s docstring for the full provenance). MOS
    # bindings carry every measured parameter onto the `X` card as of #695
    # (see `devices[].params`' `as_um2`/`ad_um2`/`ps_um`/`pd_um` above) and so
    # never reach this branch. One aggregate line per affected class (not per
    # device), mirroring `unbiased_pmos_body_nets`'s pattern just above --
    # `device_counts` is keyed by `devices[].class`, independent of `--pdk`,
    # so it is safe to consult here even though it was computed before the
    # `--pdk`-bound netlist is written below.
    if model_bindings is not None:
        for class_name in sorted(model_bindings):
            binding = model_bindings[class_name]
            if not binding.dropped_params:
                continue
            count = device_counts.get(class_name, 0)
            if count == 0:
                continue
            device_word = "device" if count == 1 else "devices"
            params_str = "/".join(binding.dropped_params)
            # `binding.subckt` is empty for a "bipolar" binding -- the real
            # subcircuit name is resolved per device, from `binding.variants`
            # (see `_select_bipolar_variant`), not fixed for the whole class
            # -- so name the *target* generically rather than print an empty
            # quoted string.
            target = f"'{binding.subckt}'" if binding.subckt else "its bound target"
            warnings.append(
                f"--pdk binds {count} '{class_name}' {device_word} onto "
                f"{target}, which has no parameter for the extractor's "
                f"measured {params_str} -- these values are "
                "dropped from the written netlist (rerun without --pdk to "
                "recover them from the bare device-class card form). See "
                "docs/cli/extract.md's 'SPICE model binding' section."
            )

    # Nets touching exactly one device terminal and no declared pin (issue
    # #596): there is no DC path through such a node from anywhere else in
    # the netlist, so a downstream simulator hits a singular matrix on it --
    # several stages past where this is structurally detectable from the
    # extracted netlist alone. Surfaced two ways: a structured
    # `single_terminal_nets[]` entry (so a caller does not have to
    # cross-reference `nets[]`/`devices[]` itself) and a matching prose
    # `warnings[]` entry, phrased more strongly for a gate terminal (almost
    # never intentional) than for source/drain/body/resistor-equivalent
    # terminals (can be a legitimate unterminated dummy tie). Each bucket
    # gets its own single aggregate `warnings[]` line with the count baked
    # in (issue #599), mirroring `_detect_unmodelled_poly_bodies`'s
    # two-message-class aggregate pattern -- one line per net would blow up
    # `warnings[]` at scale.
    single_terminal_nets = _detect_single_terminal_nets(devices, nets)
    single_terminal_gate_count = sum(
        1 for entry in single_terminal_nets if entry["terminal_kind"] == "gate"
    )
    single_terminal_other_count = len(single_terminal_nets) - single_terminal_gate_count
    if single_terminal_gate_count:
        net_word = "net" if single_terminal_gate_count == 1 else "nets"
        warnings.append(
            f"{single_terminal_gate_count} {net_word} connect to exactly "
            "one device terminal -- a MOS gate -- and are not a declared "
            "pin: these gates have no DC path from anywhere else in the "
            "netlist, so they are almost certainly unconnected inputs "
            "rather than legitimate floating nodes; resimulating this "
            "netlist directly will hit a singular matrix on these nets -- "
            "see single_terminal_nets[] for the full list."
        )
    if single_terminal_other_count:
        net_word = "net" if single_terminal_other_count == 1 else "nets"
        warnings.append(
            f"{single_terminal_other_count} {net_word} connect to exactly "
            "one device terminal -- source/drain/body/resistor-equivalent, "
            "not a gate -- and are not a declared pin; this can be a "
            "legitimate single-terminal tie (e.g. an intentionally "
            "unterminated dummy's diffusion tie), but confirm these nets "
            "have no other intended connectivity -- see "
            "single_terminal_nets[] for the full list."
        )

    # Layers carrying shapes the deck's connectivity graph never reads (issue
    # #220): geometry there is invisible to extraction, so surface it rather
    # than let it become a silent LVS mismatch downstream.
    ignored_layers = _describe_ignored_layers(path, deck)

    # A material `ignored_layers` result gets a matching `warnings[]` entry
    # (issue #666): `_describe_layers_in_set` already drops every
    # `shapes == 0` entry before it reaches `ignored_layers`, so a non-empty
    # list here is by construction "material" -- geometry that is genuinely
    # invisible to this extraction's connectivity graph, not a stray empty
    # layer declaration. Before this, `ignored_layers` was a diagnostic-only
    # field: a routed net split across an undeclared metal level extracted
    # "successfully" with no signal in `warnings[]`, the one field
    # `docs/cli/extract.md` documents as the minimal self-check every `klt`
    # command output should get. One aggregate line with the shape total
    # baked in (issue #599's pattern), not one line per layer -- mirroring
    # `metals_without_coefficient`'s `warnings[]` entry a bit further down
    # this function.
    if ignored_layers:
        layer_word = "layer" if len(ignored_layers) == 1 else "layers"
        be_word = "is" if len(ignored_layers) == 1 else "are"
        total_shapes = sum(entry["shapes"] for entry in ignored_layers)
        shape_word = "shape" if total_shapes == 1 else "shapes"
        layers_str = ", ".join(
            f"{entry['layer']}/{entry['datatype']}" for entry in ignored_layers
        )
        warnings.append(
            f"{len(ignored_layers)} {layer_word} ({layers_str}) carrying "
            f"{total_shapes} {shape_word} {be_word} outside '{deck_name}' "
            "deck's connectivity graph -- this geometry is invisible to "
            "extraction, so a net routed only through it extracts as "
            "multiple disconnected nets instead of one, which will "
            "silently mismatch a downstream `klt lvs` reference netlist -- "
            "see ignored_layers[] for the full per-layer shape counts. See "
            "docs/cli/extract.md's 'ignored_layers' field documentation."
        )

    # Layers carrying shapes the deck reads for bipolar/capacitor/resistor/
    # diode device recognition but never treats as a `metals`/`vias`
    # connectivity level (issue #619): such a layer does not appear in
    # `ignored_layers` above (it *is* read), but two nets joined only through
    # it will not merge -- exactly the gap that hid sky130's own met3/met4
    # routing-connectivity ceiling behind a clean-looking `ignored_layers`
    # report before this deck's `metals` stack grew to cover them too. Unlike
    # `ignored_layers`, this is *not* mirrored into `warnings[]`: an
    # `ExtractionDeck.device_recognition_only_layers` entry is the deck's own
    # marker/mask geometry (a resistor's `poly.res` marker, a bipolar's ID
    # mark, a MiM cap's top-plate mark) -- layers that are *never* candidate
    # connectivity levels by PDK design, so their presence is routine, not a
    # gap. Reporting them here is diagnostic context for the rare case where
    # a caller genuinely needs to distinguish "read but not merged" from
    # "never read," not a signal that something is wrong with this
    # extraction. See `ExtractionDeck.device_recognition_only_layers`'s
    # docstring and docs/cli/extract.md's "Device-recognition-only layers"
    # section.
    device_recognition_only_layers = _describe_device_recognition_only_layers(
        path, deck
    )

    mom_crosscheck_report: dict[str, Any] | None = None
    parasitics_report: dict[str, Any] | None = None
    if parasitic_nets is not None:
        ground_nets, coupled_pairs = parasitic_nets
        # `--mom-net` (issue #798): swap this one net's lumped-RC ground
        # capacitance for `klt mom`'s field-solved value *before*
        # `_inject_parasitics` reads `ground_nets` below, so both the
        # written SPICE `C` card and `parasitics.nets[]` for this net carry
        # the MoM value -- "extracted via klt mom instead of lumped RC" per
        # the epic's own Phase 1b acceptance criterion, not merely reported
        # alongside it. Every other net's entry is untouched. Validated
        # above (an unresolvable net or a solve with no metal-role geometry
        # already raised `ExtractError`), so `mom_crosscheck` here is always
        # a dict with a non-`None` `mom_capacitance_ff` when `mom_net` was
        # given.
        #
        # The entry to swap is resolved from the solve's own `net_id`, not by
        # re-matching the `--mom-net` *name* against `ground_nets` a second
        # time (issue #811) -- see `_mom_ground_entry_for_crosscheck`. A
        # layout label is not a net identity, so two by-name lookups over two
        # differently-ordered net lists are not guaranteed to select the same
        # island; keying on the id the solve already carries removes the
        # question.
        if mom_net is not None:
            assert mom_crosscheck is not None
            matched_entry = _mom_ground_entry_for_crosscheck(
                ground_nets, mom_crosscheck
            )
            if matched_entry is None:
                raise ExtractError(
                    f"--mom-net {mom_net!r} matches no net with "
                    "ground-eligible parasitics geometry in this layout"
                )
            lumped_rc_capacitance_ff = matched_entry["capacitance_ff"]
            mom_capacitance_ff = mom_crosscheck["mom_capacitance_ff"]
            matched_entry["capacitance_ff"] = mom_capacitance_ff
            delta_ff = mom_capacitance_ff - lumped_rc_capacitance_ff
            mom_crosscheck_report = {
                "net": mom_net,
                "net_id": mom_crosscheck["net_id"],
                "lumped_rc_capacitance_ff": round(lumped_rc_capacitance_ff, 6),
                "mom_capacitance_ff": round(mom_capacitance_ff, 6),
                "delta_ff": round(delta_ff, 6),
                "delta_pct": (
                    round(100.0 * delta_ff / lumped_rc_capacitance_ff, 3)
                    if lumped_rc_capacitance_ff
                    else None
                ),
                "background_permittivity": mom_crosscheck["background_permittivity"],
                "panel_size_um": mom_crosscheck["panel_size_um"],
                "panel_count": mom_crosscheck["panel_count"],
                "ground_pad_factor": mom_crosscheck["ground_pad_factor"],
                "method": (
                    "klt mom (Method of Moments) two-conductor solve between "
                    "this net's own metal-role geometry and a synthesized "
                    "ground plate, derived from the deck's own lumped-RC "
                    "coefficient table -- see docs/cli/extract.md's "
                    "'--mom-net' section for the full derivation and "
                    "reproduction steps."
                ),
                "warnings": list(mom_crosscheck["warnings"]),
            }
            warnings.extend(mom_crosscheck["warnings"])
        # `--mom-rlc-net` (issue #988, Epic #709 Phase 3a): substitute a
        # caller-supplied R/L/C for one named net's Phase 1/2 lumped-RC
        # model *before* `_inject_parasitics` reads `ground_nets` below --
        # see `run_extract`'s own `mom_rlc_net` docstring paragraph. Unlike
        # `--mom-net` above, the entry to swap is resolved by *name*
        # (matching `--critical-net`'s own convention, not `--mom-net`'s
        # `net_id`-keyed one): there is no single solved net object here to
        # key on, since the R/L/C values are opaque caller input, not the
        # output of a solve this function itself ran against one specific
        # net object. Every distinct net object sharing this net name (e.g.
        # several un-strapped islands with the same layout label) is
        # substituted the same way.
        mom_rlc_override_report: dict[str, Any] | None = None
        if mom_rlc_net is not None:
            matched_entries = [e for e in ground_nets if e["net"] == mom_rlc_net]
            if not matched_entries:
                raise ExtractError(
                    f"--mom-rlc-net {mom_rlc_net!r} matches no net with "
                    "ground-eligible parasitics geometry in this layout"
                )
            previous_resistance_ohm = sum(e["resistance_ohm"] for e in matched_entries)
            previous_capacitance_ff = sum(e["capacitance_ff"] for e in matched_entries)
            for matched_entry in matched_entries:
                if mom_rlc_resistance_ohm is not None:
                    matched_entry["resistance_ohm"] = mom_rlc_resistance_ohm
                if mom_rlc_capacitance_ff is not None:
                    matched_entry["capacitance_ff"] = mom_rlc_capacitance_ff
            mom_rlc_override_report = {
                "net": mom_rlc_net,
                "matched_net_count": len(matched_entries),
                "previous_resistance_ohm": round(previous_resistance_ohm, 4),
                "previous_capacitance_ff": round(previous_capacitance_ff, 6),
                "resistance_ohm": mom_rlc_resistance_ohm,
                "capacitance_ff": mom_rlc_capacitance_ff,
                "inductance_nh": mom_rlc_inductance_nh,
                "method": (
                    "caller-supplied value (e.g. a separate `klt mom` "
                    "Method-of-Moments solve against this net's real "
                    "geometry, Epic #701) substituted verbatim for this "
                    "net's Phase 1/2 lumped-RC ground model -- see "
                    "docs/cli/extract.md's '--mom-rlc-net' section."
                ),
            }
        if circuit is not None and (ground_nets or coupled_pairs):
            ground_net = deck.substrate_net
            parasitics_report = _inject_parasitics(
                kdb,
                circuit,
                ground_nets,
                coupled_pairs,
                ground_net,
                distributed_rc_nets=(critical_nets_set if distributed_rc else None),
                mom_rlc_inductor=(
                    (mom_rlc_net, mom_rlc_inductance_nh)
                    if mom_rlc_net is not None and mom_rlc_inductance_nh is not None
                    else None
                ),
            )
        else:
            parasitics_report = {
                "r_count": 0,
                "c_count": 0,
                "cc_count": 0,
                "l_count": 0,
                "total_resistance_ohm": 0.0,
                "total_capacitance_ff": 0.0,
                "total_coupling_capacitance_ff": 0.0,
                "total_inductance_nh": 0.0,
                "nets": [],
                # Additive field (issue #1263). This branch injected nothing
                # into the circuit at all, so there is no substrate node for
                # a DC tie to anchor either -- the block is still present
                # (schema stability), just empty.
                "substrate_dc_tie": {
                    "resistance_ohm": SUBSTRATE_DC_TIE_RESISTANCE_OHM,
                    "nets": [],
                },
            }
        # `parasitics_deck` is only non-None when `--parasitics` was given
        # (see above), which is exactly when `parasitic_nets is not None`.
        assert parasitics_deck is not None
        metal_gaps = _describe_parasitics_metal_gaps(deck, parasitics_deck)
        parasitics_report["metals_without_coefficient"] = metal_gaps
        if metal_gaps:
            levels = ", ".join(f"Metal{gap['metal_index'] + 1}" for gap in metal_gaps)
            warnings.append(
                f"'{deck_name}' deck's PARASITICS.metals has no R/C "
                f"coefficient for {levels} -- --parasitics reports zero "
                "resistance and capacitance for that metal level on every "
                "net, understating the true value. See docs/cli/extract.md's "
                "'Parasitic (RC) extraction' section."
            )
        # Additive field (issue #760): the `metals_without_coefficient`-style
        # gap report for the vertical-overlap coupling coefficient family --
        # see `_describe_parasitics_overlap_gaps`'s docstring.
        overlap_gaps = _describe_parasitics_overlap_gaps(deck, parasitics_deck)
        parasitics_report["overlap_pairs_without_coefficient"] = overlap_gaps
        if overlap_gaps:
            pairs = ", ".join(
                f"Metal{gap['lower_metal_index'] + 1}/"
                f"Metal{gap['upper_metal_index'] + 1}"
                for gap in overlap_gaps
            )
            warnings.append(
                f"'{deck_name}' deck's PARASITICS.metal_overlaps has no "
                f"vertical-overlap coupling coefficient for {pairs} -- "
                "--parasitics reports zero net-to-net coupling capacitance "
                "for that adjacent metal-level pair, understating the true "
                "value (the corresponding area still charges to ground, "
                "unlike a pair with a curated coefficient). See "
                "docs/cli/extract.md's 'Parasitic (RC) extraction' section."
            )
        # Additive field (issue #976): echoes the `--critical-net` request
        # back verbatim (as given, not sorted/deduplicated) -- `[]` when the
        # flag was never given. A name matching no net with ground-eligible
        # parasitics geometry is not an error (a caller may name several
        # candidate nets across several blocks/runs) -- flagged in
        # `warnings` instead, mirroring `--pins`' "declared name matched no
        # promoted net" convention.
        parasitics_report["critical_nets"] = (
            list(critical_nets) if critical_nets else []
        )
        if critical_nets_set:
            # `ground_nets[].net` is the unescaped identity spelling (issue
            # #1162, see `_net_identity_name`'s docstring); `critical_nets_set`
            # is caller-supplied and named using the escaped spelling this
            # module reports everywhere else, so the comparison set below
            # re-escapes each entry.
            matched_net_names = {
                spice_safe_net_name(entry["net"]) for entry in ground_nets
            }
            unmatched = sorted(critical_nets_set - matched_net_names)
            if unmatched:
                warnings.append(
                    "--critical-net name(s) "
                    f"{', '.join(repr(name) for name in unmatched)} match no "
                    "net with ground-eligible parasitics geometry in this "
                    "layout -- lateral coupling was not computed for "
                    "them. See docs/cli/extract.md's '--critical-net' "
                    "section."
                )
            if not any(parasitics_deck.metal_sidewalls):
                warnings.append(
                    f"'{deck_name}' deck's PARASITICS.metal_sidewalls curates "
                    "no lateral (same-layer) coupling coefficient for any "
                    "metal level -- --critical-net reports zero lateral "
                    "coupling capacitance for every requested net. See "
                    "docs/cli/extract.md's '--critical-net' section."
                )
        # Additive field (issue #977): `True` only when `--distributed-rc`
        # was given (always `False` otherwise, `--critical-net`-only runs
        # included) -- distinguishes "lateral coupling only" (Phase 2a) runs
        # from "lateral coupling plus distributed RC" (Phase 2b) runs
        # without a caller having to inspect individual `nets[].rc_model`
        # entries.
        parasitics_report["distributed_rc"] = bool(distributed_rc)
        if distributed_rc:
            assert critical_nets_set is not None  # validated above
            distributed_net_names = {
                entry["net"]
                for entry in parasitics_report["nets"]
                if entry.get("rc_model") == "distributed"
            }
            fell_back = sorted(
                (critical_nets_set & matched_net_names) - distributed_net_names
            )
            if fell_back:
                warnings.append(
                    "--distributed-rc name(s) "
                    f"{', '.join(repr(name) for name in fell_back)} matched "
                    "a net with fewer than 2 device terminals -- kept the "
                    "star/Gamma-shunt model for them instead of a "
                    "distributed ladder (nothing to chain). See "
                    "docs/cli/extract.md's '--distributed-rc' section."
                )
        # Additive field (issue #728, updated by #760, #976): declares the
        # parasitic model's own scope machine-readably (net-to-ground
        # capacitance plus vertical-overlap net-to-net coupling, plus
        # `--critical-net`-scoped lateral coupling; single lumped series
        # resistance per net; quasi-static) -- present on every
        # `parasitics` block regardless of whether any net actually carried
        # non-zero parasitics, since the model's scope does not depend on
        # what was found. See `PARASITIC_MODEL_SCOPE`'s docstring.
        parasitics_report["model"] = dict(PARASITIC_MODEL_SCOPE)
        # Additive field (issue #798): `None` unless `--mom-net` was given,
        # in which case it is the swap-and-measure report built above -- see
        # `run_extract`'s `mom_net` docstring paragraph and
        # `docs/cli/extract.md`'s `--mom-net` section.
        parasitics_report["mom_crosscheck"] = mom_crosscheck_report
        # Additive field (issue #988, Epic #709 Phase 3a): `None` unless
        # `--mom-rlc-net` was given, in which case it is the substitution
        # report built above -- see `run_extract`'s `mom_rlc_net` docstring
        # paragraph and `docs/cli/extract.md`'s `--mom-rlc-net` section.
        parasitics_report["mom_rlc_override"] = mom_rlc_override_report

    # `--spef` (issue #948): translate the just-built `parasitics_report`
    # into a SPEF file at `spef_output` -- see `_write_spef`'s docstring for
    # the exact shape and its net-name-only correlation scope. Only reached
    # when `parasitics_report is not None` (guaranteed above: `spef_output`
    # given requires `parasitics=True`, which is exactly when
    # `parasitic_nets is not None`).
    spef_path: str | None = None
    if spef_output is not None:
        assert parasitics_report is not None
        spef_path = spef_output
        spef_out_dir = os.path.dirname(os.path.abspath(spef_path))
        try:
            os.makedirs(spef_out_dir, exist_ok=True)
        except OSError as exc:
            raise ExtractError(
                f"cannot create output directory {spef_out_dir}: {exc}"
            ) from exc
        _write_spef(
            spef_path,
            design_name=top_cell_name,
            klt_version=_klt_version(),
            parasitics_report=parasitics_report,
            port_names=(entry["name"] for entry in nets if entry["pin"]),
            # Issue #961: real cell-instance `*CONN`/`*RES` correlation from
            # the routed DEF's own `NETS` section -- `None` (the default)
            # falls back to the pre-#961 port-only behavior.
            net_instance_pins=def_net_connections,
        )

    writer = (
        kdb.NetlistSpiceWriter(create_model_binding_delegate(model_bindings))
        if model_bindings is not None
        else kdb.NetlistSpiceWriter()
    )
    writer.use_net_names = True
    netlist_description = f"extracted by klt extract --deck {deck_name}"
    if parasitics_report is not None:
        netlist_description += "\n" + _parasitic_model_header_comment()
    try:
        netlist.write(netlist_path, writer, netlist_description)
    except Exception as exc:
        raise ExtractError(f"could not write netlist '{netlist_path}': {exc}") from exc

    netlist_sha256 = sha256_file(netlist_path)

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "file": path,
        "deck": deck_name,
        "top": top_cell_name,
        "dbu_um": dbu_um,
        "netlist_path": netlist_path,
        "netlist_sha256": netlist_sha256,
        "status": "extracted",
        "device_count": len(devices),
        "net_count": len(nets),
        "pin_count": sum(1 for net in nets if net["pin"]),
        "device_counts": dict(sorted(device_counts.items())),
        "dummy_devices_dropped": dummy_devices_dropped,
        "ignored_layers": ignored_layers,
        # Additive field (issue #619): always a list, empty when no layer is
        # read for device recognition only -- see run_extract's docstring and
        # `ExtractionDeck.device_recognition_only_layers` for the field's
        # full meaning.
        "device_recognition_only_layers": device_recognition_only_layers,
        "device_classes": list(deck.device_classes),
        "devices": devices,
        "nets": nets,
        "warnings": warnings,
        # Additive field (issue #293): always a list, empty when the layout
        # draws no reserved-annotation-layer geometry -- see run_extract's
        # docstring for the field's full meaning.
        "black_box_regions": black_box_regions,
        # Additive field (issue #620): always a list, empty unless
        # `abstract_cell_patterns` (--abstract-cells) matched at least one
        # instantiated cell -- see run_extract's docstring for the field's
        # full meaning.
        "abstracted_cells": abstracted_cells,
        # Additive field (issue #324): always a list, empty when `warnings`
        # carries no unmodelled-device entry -- see run_extract's docstring
        # and `_detect_unmodelled_poly_bodies` for the field's full meaning.
        "unmodelled_poly": unmodelled_poly,
        # Additive field (issue #470): always a list, empty when no net
        # carries more than one KLayout-assigned label -- see
        # `_detect_merged_net_labels` and the comment above where it is
        # computed for the field's full meaning.
        "merged_net_labels": merged_net_labels,
        # Additive field (issue #552): always a list, empty when this deck
        # registers no voltage-domain marker or none of it overlaps
        # extracted MOS geometry -- see run_extract's docstring and
        # `_detect_voltage_domain_overlap` for the field's full meaning.
        "voltage_domain_warnings": voltage_domain_warnings,
        # Additive field (issue #555): always a list, empty when no PMOS
        # device's body net is the anonymous, KLayout-synthesized kind -- see
        # run_extract's docstring and `_detect_unbiased_pmos_body_nets` for
        # the field's full meaning.
        "unbiased_pmos_body_nets": unbiased_pmos_body_nets,
        # Additive field (issue #596): always a list, empty when every net
        # either has zero or 2+ device terminals, or is a declared pin -- see
        # run_extract's docstring and `_detect_single_terminal_nets` for the
        # field's full meaning.
        "single_terminal_nets": single_terminal_nets,
        # Additive field (issue #676): always a list, empty when every
        # routing-stack (metals/vias) shape joins an extracted net -- see
        # run_extract's docstring and `_detect_dead_metal` for the field's
        # full meaning. Computed inside `_extract_netlist` (it needs the live
        # `LayoutToNetlist` shape database), which also appends its aggregate
        # `warnings[]` entry.
        "dead_metal": dead_metal,
        # Additive field (issue #1018): always a list, empty unless
        # `matched_device_groups` (--matched-group) was given -- see
        # run_extract's docstring and `_describe_matched_device_groups` for
        # the field's full meaning.
        "matched_device_groups": matched_device_groups_report,
    }
    if pdk_info is not None:
        result["pdk"] = {
            "variant": pdk_info["variant"],
            # {path, scope} (issue #1376), not the raw `--pdk-root` argument
            # -- a PDK install is external to the invoking repo by
            # definition, so the old raw-path shape baked a host-specific
            # absolute path (possibly a username) into any committed
            # `--format json` evidence report. `provenance.pdk` below
            # already carries this PDK's reproducible identity
            # (name/source/version) without a path.
            "root": env_provenance.repo_relative_path(pdk_info["root"]),
            "version": pdk_info["version"],
        }
    else:
        result["pdk"] = None

    result["provenance"] = build_provenance(
        deck_name=deck_name,
        deck_path=deck_source_path(deck_name),
        pdk=pdk_info,
        input_path=path,
        deck_options=deck_options,
    )

    # Additive, independently-optional field (issue #216 addendum): `null`
    # unless `--parasitics` was given, a `parasitics` summary block otherwise.
    result["parasitics"] = parasitics_report

    # Additive field (issue #948): `null` unless `--spef` was given, the
    # resolved SPEF path otherwise -- see `run_extract`'s `spef_output`
    # docstring paragraph and `docs/cli/extract.md`'s "SPEF export" section.
    result["spef_path"] = spef_path

    return result


# --------------------------------------------------------------------------- #
# --check / --rerun: verify a previously committed report (issue #1149)
# --------------------------------------------------------------------------- #


def check_extract_report(report_path: str) -> dict[str, Any]:
    """``klt extract --check`` (cheap mode, issue #1149): verify a previously
    committed ``klt extract --format json`` report at ``report_path`` still
    reproduces, without re-running the extraction engine at all.

    Mirrors ``klt drc --check``'s ``check_drc_report()`` (``drc.py``) and
    ``klt lvs --check``'s ``check_lvs_report()`` (``lvs.py``) exactly, wiring
    ``klt extract`` up to the same shared ``_report_verify.py`` machinery
    issue #1106 built for those two verbs: re-hashes the input layout stream
    (``committed["file"]``) and the deck
    (:func:`~klayout_tools.decks.deck_source_path`, resolved from
    ``committed["provenance"]["deck"]["name"]``) and compares each against
    the ``sha256:``-prefixed digest already recorded in
    ``provenance.input.content_hash``/``provenance.deck.content_hash``
    (:func:`klayout_tools._provenance._content_hash`) -- reusing
    :func:`klayout_tools._provenance.sha256_file`, never reimplementing
    hashing. Returns the shared ``--check`` payload built by
    :func:`klayout_tools._report_verify.build_check_result`: ``status:
    "match"`` when both hashes agree, ``"drifted"`` (naming which one moved)
    otherwise.

    This is what surfaces a deck rebuild that silently changed
    device-recognition behavior (e.g. gf180mcu's substrate/well-tap
    derivation, issue #1149) underneath a previously-committed extraction:
    since a curated deck is a plain Python module (``decks/gf180mcu.py``),
    *any* byte change to it -- including a device-recognition change --
    changes ``content_hash``, so a caller re-checking a committed report
    against a newer deck build sees ``status: "drifted"`` even though the
    reported ``klt --version``/``klayout_version`` may be unchanged.

    A recorded hash that is itself ``None`` (a report predating
    ``provenance.input``/``deck.content_hash``) never counts as a match --
    see :func:`klayout_tools._report_verify.hash_check`'s docstring. ``klt
    extract --deck`` is required for every run, so ``provenance.deck`` is
    always populated for a genuine committed report; an unresolvable deck
    name (e.g. the deck module was since renamed/removed) hashes to ``None``
    via :func:`~klayout_tools.decks.deck_source_path`, which likewise never
    counts as a match.

    Raises :class:`ExtractError` for a missing/unparseable committed report
    (:func:`klayout_tools._report_verify.load_committed_report`) -- never a
    traceback.
    """
    committed = _load_committed_report(report_path, ExtractError)
    deck_name = get_path(committed, ("provenance", "deck", "name"))
    checks = [
        hash_check(
            "provenance.input.content_hash",
            get_path(committed, ("provenance", "input", "content_hash")),
            _content_hash(committed.get("file")),
        ),
        hash_check(
            "provenance.deck.content_hash",
            get_path(committed, ("provenance", "deck", "content_hash")),
            _content_hash(deck_source_path(deck_name) if deck_name else None),
        ),
    ]
    return build_check_result(report_path=report_path, checks=checks)


def rerun_extract_report(report_path: str) -> dict[str, Any]:
    """``klt extract --check <report> --rerun`` (full mode, issue #1149):
    verify a previously committed ``klt extract --format json`` report at
    ``report_path`` by actually re-running the extraction it describes and
    diffing the fresh report against the committed one.

    Re-runs :func:`run_extract` against the ``file``/``deck``/``top`` the
    committed report itself names, plus ``provenance.deck.options`` (issue
    #595's ``--deck-option`` selections) when present, writing the fresh
    netlist back to the same ``netlist_path`` the committed report recorded
    (so that field, too, is a meaningful comparison rather than an
    incidental path difference). **Known limitation**, mirroring ``klt lvs
    --check``'s ``rerun_lvs_report()`` (``lvs.py``): every *other* optional
    ``klt extract`` flag (``--parasitics``, ``--mom-net``, ``--spef``,
    ``--critical-net``, ``--distributed-rc``, ``--def-net-names``,
    ``--def-net-connections``, ``--mom-rlc-*``, ``--top-cell-pins``,
    ``--pins``, ``--defer-resistor-fixed-offset``, ``--abstract-cells``,
    ``--abstract-cell-lef``, ``--matched-group``, ``--pdk``/``--pdk-root``)
    is never echoed anywhere in the response, so none of them can be
    reconstructed here -- a committed report produced with any of those will
    legitimately (and unhelpfully) show drift in the corresponding
    fields/blocks under ``--rerun``. Use ``--check`` (cheap mode) instead
    when any of these apply.

    Diffs the fresh report against the committed one via
    :func:`klayout_tools._report_verify.diff_verdict_fields`, excluding
    :data:`klayout_tools._report_verify.VOLATILE_PROVENANCE_PATHS`
    (``provenance.klt_version``/``klayout_version``/``pdk.version``).
    ``status: "drifted"`` names every other field that changed, including a
    changed ``device_count``/``devices``/``nets``/etc. (the
    extraction-outcome-changed case, e.g. a deck's substrate/well-tap
    recognition change, issue #1149) as well as a changed
    ``provenance.input.content_hash``/``provenance.deck.content_hash`` (the
    input-moved case ``--check`` also catches, redundantly but harmlessly
    here since this mode always re-hashes as a side effect of re-running).

    Raises :class:`ExtractError` for a missing/unparseable committed report,
    a report missing ``file``/``deck`` to rerun, or any error the rerun
    itself raises (bad file, unknown deck, engine error) -- never a
    traceback.
    """
    committed = _load_committed_report(report_path, ExtractError)
    file_path = committed.get("file")
    if not file_path:
        raise ExtractError(
            f"committed report has no 'file' field to rerun: {report_path}"
        )
    deck_name = committed.get("deck")
    if not deck_name:
        raise ExtractError(
            f"committed report has no 'deck' field to rerun: {report_path}"
        )

    deck_options = get_path(committed, ("provenance", "deck", "options"))

    fresh = run_extract(
        file_path,
        deck_name,
        output=committed.get("netlist_path"),
        top=committed.get("top"),
        deck_options=deck_options,
    )
    return build_rerun_result(report_path=report_path, committed=committed, fresh=fresh)


def extract_netlist_from_layout(
    path: str,
    deck_name: str,
    top: str | None = None,
    parasitics_deck: ParasiticsDeck | None = None,
    top_cell_pins_only: bool = False,
    declared_pins: frozenset[str] | None = None,
    apply_resistor_fixed_offset: bool = True,
    deck_options: Mapping[str, str] | None = None,
    abstract_cell_patterns: tuple[str, ...] = (),
    abstract_cell_lef_paths: tuple[str, ...] = (),
    mom_net: str | None = None,
    mom_background_permittivity: float = MOM_CROSSCHECK_BACKGROUND_PERMITTIVITY,
    def_net_names: bool = False,
    critical_nets: frozenset[str] | None = None,
    def_pins: frozenset[str] | None = None,
) -> tuple[
    kdb.Netlist,
    str,
    float,
    list[str],
    tuple[list[dict[str, Any]], list[dict[str, Any]]] | None,
    list[dict[str, Any]],
    int,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any] | None,
]:
    """Core extraction: read ``path``, resolve ``deck_name`` and the top
    cell, and run flat device + connectivity extraction. Returns
    ``(netlist, top_cell_name, dbu_um, warnings, parasitic_nets,
    black_box_regions, dummy_devices_dropped, unmodelled_poly,
    voltage_domain_warnings, abstracted_cells, dead_metal, mom_crosscheck)``.

    ``abstract_cell_patterns``/``abstract_cell_lef_paths`` (the
    ``--abstract-cells``/``--abstract-cell-lef`` flags, issue #620): when
    ``abstract_cell_patterns`` is non-empty, every instantiated cell whose
    name matches one of the given ``fnmatch`` glob patterns is treated as a
    black box -- its own device-recognition geometry is erased from the
    layout before extraction (see :func:`_erase_abstracted_cell_geometry`)
    and it becomes a pin-only ``.SUBCKT``/``X`` instance in the returned
    netlist instead of contributing devices to the flat top-level circuit
    (see :func:`_wire_abstract_cells`). ``abstracted_cells`` is the JSON
    response's field: one entry per distinct matched cell type, each
    ``{"cell", "instance_count", "pin_count", "resolution_source",
    "lef_path"}``. Always a list, empty when ``abstract_cell_patterns`` is
    empty (the default) -- byte-identical to today's behavior in that case.
    ``abstract_cell_lef_paths`` is consulted only as the *fallback* pin
    source, when a matched cell type draws no in-cell pin label -- see
    :func:`_resolve_abstract_cell_pins`.

    ``deck_options`` (issue #595): forwarded to
    :func:`~klayout_tools.decks.get_extraction_deck` -- selects a
    caller-visible sheet-rho flavour for any resistor family whose
    ``flavour_option`` it names. ``None``/empty resolves the deck unchanged.
    See :func:`run_extract`'s docstring for the full contract.

    ``def_net_names`` (issue #951): forwarded to ``_extract_netlist`` --
    ``True`` names routed nets from the DEF net name KLayout's LEF/DEF reader
    left on their geometry as GDS shape property
    :data:`_DEF_NET_NAME_PROPERTY_ID`, rather than from text labels. Off by
    default (unchanged behavior). See :func:`run_extract` for the full
    rationale.

    ``apply_resistor_fixed_offset`` (issue #559): forwarded to
    ``_extract_netlist`` -- ``True`` (the default) applies each opted-in
    resistor device class's ``fixed_offset_ohm`` correction here, at
    extraction time (unchanged behavior). ``klt lvs``'s
    ``options.combine_devices`` path passes ``False`` and applies the
    correction itself, once, after combining -- see
    :func:`_extract_netlist` and :func:`apply_resistor_fixed_offset_corrections`.

    ``voltage_domain_warnings`` (issue #552) flags extracted MOS device
    geometry that overlaps a voltage-domain marker layer this deck does not
    model the scoping of (e.g. gf180mcu's ``Dualgate`` 55/0) -- see
    :func:`_detect_voltage_domain_overlap`. A matching prose entry is also
    appended to ``warnings``. Always a list, empty for a deck that registers
    no such marker or a layout that draws none of it overlapping MOS
    geometry.

    ``top_cell_pins_only`` (issue #291): when ``True``, only labels drawn
    directly in the top cell are promoted to top-level pins -- a net named
    solely by a label found below an instance boundary keeps its name but
    stays internal. Independent of the flag, ``warnings`` gains an entry
    whenever a below-top label named a promoted pin. See :func:`run_extract`
    for the full rationale.

    ``declared_pins`` (issue #514): when given, every promoted pin *not*
    named in this set is demoted back to an internal net (it keeps its
    name). ``None`` skips this reconciliation. See :func:`run_extract` for
    the full rationale.

    ``def_pins`` (issue #1390): the automatic, DEF-merge-aware counterpart
    to ``declared_pins`` -- forwarded to :func:`_extract_netlist`, which
    keeps a promoted net whenever *any* of its comma-joined component
    labels is in this set (not only an exact whole-name match, since a
    DEF-merged layout's net names routinely collide two or more labels onto
    one net). ``None`` skips this reconciliation. See :func:`run_extract`
    for the full rationale.

    ``parasitics_deck`` is optional: when ``None`` (the default, and what
    ``klt lvs``'s inline-extraction path always passes -- LVS is topological
    and takes no parasitics), no per-net RC geometry is computed and the
    returned ``parasitic_nets`` is ``None``. When a
    :class:`~klayout_tools.decks.ParasiticsDeck` is given, ``parasitic_nets``
    is the ``(ground_nets, coupled_pairs)`` 2-tuple :func:`_compute_parasitics`
    returns: ``ground_nets`` a list of ``{"net", "resistance_ohm",
    "capacitance_ff"}`` dicts (one per net carrying ground-eligible
    geometry), ``coupled_pairs`` a list of ``{"net_a", "net_b",
    "capacitance_ff", "levels", "lateral_levels"}`` dicts (one per distinct
    net pair with non-zero vertical-overlap coupling capacitance, issue
    #760, and/or -- when ``critical_nets`` names one side of the pair --
    lateral coupling capacitance, issue #976) -- both computed from
    ``LayoutToNetlist.polygons_of_net`` per-net/per-layer geometry. The
    caller (:func:`run_extract`) injects them into the netlist as ``R``/``C``
    devices before writing. The netlist itself is unchanged by this
    computation (the parasitics are returned as data, not yet injected).

    ``critical_nets`` (``klt extract --critical-net``, repeatable, issue
    #976): forwarded to :func:`_extract_netlist`/:func:`_compute_parasitics`
    -- a same-layer net pair only gets lateral coupling computed when at
    least one side's name is in this set. ``None``/empty (the default)
    skips the lateral pass entirely, byte-identical to before this feature
    existed.

    ``mom_net``/``mom_background_permittivity`` (``klt extract --mom-net
    <net>``, issue #798) forward straight to :func:`_extract_netlist`, which
    computes ``mom_crosscheck`` alongside ``parasitic_nets`` for the same
    "``l2n`` must still be alive" reason -- see its docstring. ``None`` (the
    default) skips the cross-check entirely, ``mom_crosscheck`` is then
    always ``None`` too, byte-identical to before this feature existed.

    Shared by :func:`run_extract` (this module, which additionally writes the
    netlist to disk and builds the ``devices``/``nets`` convenience view) and
    ``klt lvs``'s inline-extraction path (``lvs.py``'s ``layout.file`` +
    ``layout.deck`` request shape, per
    ``docs/design/lvs-extraction-spike.md`` section 2b), which composes this
    with ``NetlistComparer`` instead of ``NetlistSpiceWriter`` -- no need to
    round-trip through a written SPICE file just to compare it.

    Raises :class:`ExtractError` for a bad file, unknown deck, invalid
    ``deck_options`` entry, or missing/ambiguous top cell -- identical error
    semantics to ``run_extract``.
    """
    if not os.path.exists(path):
        raise ExtractError(f"file not found: {path}")
    if os.path.isdir(path):
        raise ExtractError(f"not a file: {path}")

    try:
        deck = get_extraction_deck(deck_name, deck_options)
    except (UnknownExtractionDeckError, InvalidDeckOptionError) as exc:
        raise ExtractError(str(exc)) from exc

    # Imported lazily (after the cheap checks above) so `klt --version` and
    # argument parsing never pay the cost of loading the KLayout database
    # module -- same discipline as `_layout.load_layout`.
    import klayout.db as kdb

    layout = kdb.Layout()
    try:
        layout.read(path)
    except Exception as exc:  # klayout raises RuntimeError for bad/unknown streams
        raise ExtractError(f"could not read layout '{path}': {exc}") from exc

    top_cell = resolve_top_cell(layout, top, ExtractError, path=path)

    # `--abstract-cells` (issue #620): resolved *before* `_extract_netlist`
    # runs, by mutating `layout` in place -- see
    # `_erase_abstracted_cell_geometry`'s docstring for why erasing each
    # matched cell type's own device-recognition geometry here (rather than
    # masking `Region` objects deep inside `_extract_netlist`) is both
    # simpler and correct for every device class, including ones
    # `_extract_netlist` re-reads straight from `layout` (bipolar/diode/
    # capacitor), not just the ones it threads through local `Region`
    # variables (MOS/resistor). `lef_macros` is loaded once here (an
    # `--abstract-cell-lef` path is a filesystem read, not layout data),
    # even though it is consulted per matched cell type inside
    # `_wire_abstract_cells`.
    abstract_instances: list[tuple[int, kdb.ICplxTrans]] = []
    lef_macros: dict[str, tuple[str, dict[str, list[dict[str, Any]]]]] = {}
    abstract_cell_local_candidates: dict[int, dict[str, list[kdb.Point]]] = {}
    if abstract_cell_patterns:
        abstract_instances = _collect_abstract_instances(
            layout, top_cell, abstract_cell_patterns
        )
        if abstract_instances:
            mask_layers = _abstract_cell_mask_layers(deck)
            matched_cell_indices = dict.fromkeys(
                cell_index for cell_index, _trans in abstract_instances
            )
            # Computed *before* erasure (issue #1183): once
            # `_erase_abstracted_cell_geometry` clears each matched cell
            # type's own device-recognition geometry below, the
            # poly/diffusion/contact connectivity that could tie a
            # disjoint-but-electrically-equivalent metal fragment to an
            # in-cell pin label is gone for good -- see
            # `_local_pin_candidate_points`'s docstring for the full
            # derivation (confirmed against the real gf180-trng `clkload13`
            # case this issue reports).
            abstract_cell_local_candidates = {
                cell_index: _local_pin_candidate_points(
                    layout, layout.cell(cell_index), deck
                )
                for cell_index in matched_cell_indices
            }
            _erase_abstracted_cell_geometry(layout, matched_cell_indices, mask_layers)
        if abstract_cell_lef_paths:
            lef_macros = _load_abstract_cell_lefs(abstract_cell_lef_paths)

    (
        netlist,
        warnings,
        parasitic_nets,
        black_box_regions,
        dummy_devices_dropped,
        unmodelled_poly,
        abstracted_cells,
        dead_metal,
        mom_crosscheck,
    ) = _extract_netlist(
        layout,
        top_cell,
        deck,
        parasitics_deck,
        top_cell_pins_only=top_cell_pins_only,
        declared_pins=declared_pins,
        apply_resistor_fixed_offset=apply_resistor_fixed_offset,
        abstract_cell_patterns=abstract_cell_patterns,
        abstract_instances=abstract_instances,
        lef_macros=lef_macros,
        abstract_cell_local_candidates=abstract_cell_local_candidates,
        mom_net=mom_net,
        mom_background_permittivity=mom_background_permittivity,
        def_net_names=def_net_names,
        critical_nets=critical_nets,
        def_pins=def_pins,
    )

    # Voltage-domain marker overlap (issue #552): computed after the main
    # extraction pass, against the same `layout`/`top_cell`/`deck` it just
    # used, so this stays a purely additive diagnostic layered on top of an
    # otherwise-unchanged extraction -- no rule threshold or model binding
    # changes because of it. See `_detect_voltage_domain_overlap`'s
    # docstring for the exact interacting-geometry gate.
    (
        voltage_domain_prose_warnings,
        voltage_domain_warnings,
    ) = _detect_voltage_domain_overlap(layout, top_cell, deck, deck_name)
    warnings = warnings + voltage_domain_prose_warnings

    return (
        netlist,
        top_cell.name,
        layout.dbu,
        warnings,
        parasitic_nets,
        black_box_regions,
        dummy_devices_dropped,
        unmodelled_poly,
        voltage_domain_warnings,
        abstracted_cells,
        dead_metal,
        mom_crosscheck,
    )


def _default_output_path(path: str) -> str:
    """``<file>`` with its extension replaced by ``.spice`` (spike section 2a)."""
    stem, _ext = os.path.splitext(path)
    return f"{stem}.spice"


#: Wall-clock budget (seconds) for the ``klayout`` subprocess
#: :func:`run_extract_klayout_engine` launches -- mirrors
#: ``drc.py``'s own ``KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S`` (same value, a
#: separate module-level constant rather than a cross-module import so
#: ``extract.py``/``drc.py`` stay independent of each other, matching this
#: repo's existing "no cross-import between the two engine-wrapper modules"
#: shape).
EXTRACT_KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S = 300.0

#: SPICE device-parameter names ``sky130.lvs``'s own custom
#: ``SubcircuitModels`` writer (a ``RBA::NetlistSpiceWriterDelegate``) emits
#: as *lengths* (microns) -- see :func:`run_extract_klayout_engine`'s
#: docstring, "Unit round-trip" for the empirically-verified reason a
#: generic re-read needs to undo a x1e6 scale for exactly these names.
_NATIVE_LVS_LENGTH_PARAMS = frozenset({"L", "W", "P", "PS", "PD"})

#: The same round-trip issue for *area* parameters (microns^2), needing a
#: x1e12 undo instead.
_NATIVE_LVS_AREA_PARAMS = frozenset({"A", "AS", "AD"})


def run_extract_klayout_engine(
    path: str,
    lvs_deck_file: str,
    top: str | None = None,
    timeout_s: float = EXTRACT_KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S,
    extra_rd: Mapping[str, str] | None = None,
    bare_length_area_units: bool = True,
) -> dict[str, Any]:
    """Run a PDK-native KLayout LVS-DSL rule-deck script's own **device
    extraction** (``lvs_deck_file``, typically resolved via
    :func:`klayout_tools.pdk.lvs_deck_file`) against the layout at ``path``,
    via the standalone ``klayout`` application binary -- the sky130
    device-extraction cross-check oracle for issue #869 (Epic #711 Phase
    2c), the LVS-device-extraction counterpart of ``drc.py``'s
    ``run_drc_klayout_engine`` (issue #565/#747). See ``docs/cli/extract.md``,
    "sky130 native-deck (``sky130.lvs``) LVS device-extraction cross-check"
    for the full writeup this docstring summarises, and that module's own
    docstring for the DRC-side precedent this one mirrors structurally.

    A PDK's ``.lvs`` script is written to run a *complete* LVS flow --
    device extraction followed by ``compare`` against a reference schematic
    -- and hard-requires a schematic to be present at all (``align``, which
    the script always reaches, raises ``RuntimeError`` immediately without
    one; verified against a real ``sky130.lvs`` for this issue). This
    function is not interested in the compare *verdict* (there is no
    trustworthy independent reference schematic to compare against here --
    the entire point is testing *extraction* agreement) -- it only wants the
    extracted device netlist the script's own ``extract_devices``/
    ``target_netlist`` machinery produces along the way. So it always
    supplies a trivial synthesized stub schematic (an empty
    ``.SUBCKT <top> / .ENDS <top>``, named after the resolved top cell) purely
    to satisfy that hard requirement, and drives three ``-rd`` globals the
    script itself reads to keep the written netlist un-simplified/un-pruned
    relative to the reference it will (deliberately, harmlessly) fail to
    match:

    - ``net_only=true`` / ``top_lvl_pins=true`` -- disables the script's own
      default ``netlist.simplify`` pass (verified for this issue: with
      ``SIMPLIFY`` -- the script's default when neither flag is set --
      the written netlist came back with an empty top-level ``.SUBCKT``,
      *zero* devices, for every fixture tried, including ones the script's
      own extraction logging confirmed it had recognised a device on;
      ``net_only``/``top_lvl_pins`` avoid that pruning) and promotes every
      labelled net to a top-level pin (needed for :func:`resolve_top_cell`-
      selected fixtures whose only pins are drawn labels, mirroring how this
      module's own compiled-deck path always promotes them).

    Invokes::

        klayout -b -r <lvs_deck_file> -rd input=<path> -rd report=<tmp>.lvsdb \\
            -rd target_netlist=<tmp>.cir -rd schematic=<tmp-stub>.cir \\
            -rd net_only=true -rd top_lvl_pins=true [-rd <extra_rd key>=<value> ...]

    ``extra_rd`` (issue #904, Epic #711 Phase 3a) appends additional
    ``-rd key=value`` pairs after the six standard ones above -- needed for a
    native deck whose own variant-selection globals default to a stack this
    repo's compiled deck does not model. For example, gf180mcu's
    ``gf180mcu.lvs`` defaults ``$metal_level`` to ``'6LM'``
    (``topmin1_metal`` = ``Metal5``), but ``decks/gf180mcu.py``'s own MiM
    capacitor models the 5LM variant (``Metal4`` as the MiM stack's bottom
    plate, per its own docstring) -- cross-checking that entry needs
    ``extra_rd={"metal_level": "5LM"}`` to point the native deck's own
    variant selection at the same stack the compiled deck assumes. ``None``
    (the default) appends nothing, matching this function's pre-#904
    behaviour exactly.

    Mirrors ``run_drc_klayout_engine``'s completion discipline exactly:

    - **Binary resolution**: no ``shutil.which`` precheck -- ``subprocess.run``
      is attempted directly and a ``FileNotFoundError`` is caught and
      re-raised as an actionable :class:`ExtractError`.
    - **Timeout**: ``subprocess.TimeoutExpired`` -> :class:`ExtractError`.
    - **Never trusts the exit code.** The script's own ``compare`` against
      the synthesized empty-stub schematic is *expected* to report a
      mismatch (``exit(1)``, logged as ``"ERROR : Netlists don't match"``)
      on every real fixture -- that is not a failure of this function, it is
      the deliberate, harmless side effect of supplying a stub instead of a
      real reference. The *target-netlist file's own presence* is this
      function's only trustworthy completion signal (mirroring
      ``run_drc_klayout_engine``'s "report file's own presence" check and
      ``lvs.py``'s ``_run_netgen_lvs`` "no log file at all" check) -- a
      missing file (the script errored out *before* completing extraction,
      e.g. a malformed ``lvs_deck_file``) is :class:`ExtractError`, carrying
      klayout's own stdout/stderr.

    **Unit round-trip.** ``sky130.lvs``'s own SPICE writer (a custom
    ``RBA::NetlistSpiceWriterDelegate``) emits each device parameter's raw
    internal value directly (already in klayout's native micron/micron^2
    representation, since this deck runs with ``device_scaling`` off) --
    *not* rescaled to plain-SPICE meter/meter^2 convention, and with no
    engineering-notation unit suffix on the written number at all (a bare
    ``L=0.4``, not ``L=0.4U``). Reading that file back through a plain
    ``kdb.NetlistSpiceReader()`` (no custom delegate) re-applies the
    *opposite* assumption for a bare, suffix-less number: it treats a
    MOS/resistor device class's declared length-typed parameters
    (``L``/``W``/``P``/``PS``/``PD``) as meters and rescales by 1e6 to store
    them internally (and area-typed ``A``/``AS``/``AD`` by 1e12) -- verified
    empirically for issue #869 (a written ``L=0.4`` round-trips as
    ``400000.0``, a written ``A=6`` as ``6000000000000.0``; ``R``/``C`` are
    untouched, confirming only the length/area-typed parameters carry this
    reader-side rescale). ``bare_length_area_units=True`` (the default,
    preserving pre-#904 behaviour) undoes exactly that known, fixed factor
    (:data:`_NATIVE_LVS_LENGTH_PARAMS` / 1e6, :data:`_NATIVE_LVS_AREA_PARAMS`
    / 1e12) on read-back, so ``devices[].params`` reports the same
    micron/micron^2/ohm/farad convention :func:`run_extract`'s own
    ``devices[].params`` does, comparable value-for-value.

    **Not every native writer shares this bare-number quirk** (issue #904,
    Epic #711 Phase 3a): gf180mcu's ``gf180mcu.lvs`` uses KLayout's own
    built-in ``write_spice(...)`` (no custom delegate), which *does* emit an
    explicit engineering-notation unit suffix on length/area values
    (``L=0.4U``, ``AS=0.8P``) -- and ``kdb.NetlistSpiceReader()`` parses that
    suffix correctly on read-back, so the value comes back already in the
    same micron/micron^2 convention :func:`run_extract` uses, needing *no*
    further rescale at all (verified empirically for issue #904: a written
    ``L=0.4U`` round-trips as plain ``0.4``, not ``400000.0``). Pass
    ``bare_length_area_units=False`` for a native deck confirmed to write
    unit-suffixed numbers this way, to skip the sky130-specific undo instead
    of silently mis-scaling every length/area parameter by a further,
    incorrect factor of 1e6/1e12.

    Returns a dict shaped closely enough to :func:`run_extract`'s own
    ``devices``/``device_counts`` fields to compare directly (see
    ``docs/cli/extract.md``): ``{"schema_version": 1, "file": path, "deck":
    lvs_deck_file, "engine": "klayout", "top": <resolved top cell name>,
    "device_count": int, "device_counts": {<class name>: int, ...},
    "devices": [{"name": str, "class": str, "params": {<name>: float,
    ...}}, ...]}``. ``device_class`` names come back **upper-cased**
    (``kdb.NetlistSpiceReader``'s own case-folding of every SPICE model
    name it reads, a pre-existing, separately-documented quirk -- see
    ``lvs.py``'s netlist-reading notes) -- a caller comparing against this
    repo's own lower-case ``RuleProvenance.rule_id``/device-class-name
    strings must compare case-insensitively. Unlike :func:`run_extract`,
    there is no ``nets``/``warnings``/``coverage`` -- this is a narrow
    extraction-agreement oracle, not a second general-purpose engine.

    Raises :class:`ExtractError` for every failure mode above, plus a
    missing/unreadable ``path`` or ``lvs_deck_file``, or an unparseable
    extracted-netlist file (checked before/after the subprocess the same
    fail-fast way :func:`run_extract` and ``run_drc_klayout_engine`` do).
    """
    if not os.path.isfile(lvs_deck_file):
        raise ExtractError(f"LVS deck file not found: {lvs_deck_file}")

    layout = load_layout(path, ExtractError)
    top_cell = resolve_top_cell(layout, top, ExtractError, path=path)
    top_cell_name = top_cell.name

    work_dir = tempfile.mkdtemp(prefix="klt-extract-klayout-")
    try:
        stub_schematic_path = os.path.join(work_dir, "stub_schematic.cir")
        with open(stub_schematic_path, "w", encoding="utf-8") as handle:
            handle.write(f".SUBCKT {top_cell_name}\n.ENDS {top_cell_name}\n")

        report_path = os.path.join(work_dir, "report.lvsdb")
        netlist_path = os.path.join(work_dir, "extracted.cir")
        cmd = [
            "klayout",
            "-b",
            "-r",
            lvs_deck_file,
            "-rd",
            f"input={path}",
            "-rd",
            f"report={report_path}",
            "-rd",
            f"target_netlist={netlist_path}",
            "-rd",
            f"schematic={stub_schematic_path}",
            "-rd",
            "net_only=true",
            "-rd",
            "top_lvl_pins=true",
        ]
        for key, value in (extra_rd or {}).items():
            cmd.extend(["-rd", f"{key}={value}"])
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s
            )
        except FileNotFoundError as exc:
            raise ExtractError(
                "could not launch klayout: binary not found on PATH. "
                "Install KLayout (https://www.klayout.de/build.html) or "
                "use run_extract (the compiled deck) instead. "
                f"({exc})"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ExtractError(
                f"klayout did not complete within {timeout_s}s (raise "
                "timeout_s to allow more time)"
            ) from exc

        if not os.path.isfile(netlist_path):
            # Never trust the exit code alone (see this function's
            # docstring) -- no netlist file at all means the deck script
            # errored out before completing extraction, not merely that its
            # (expected) compare-against-the-stub reported a mismatch.
            raise ExtractError(
                "klayout did not produce an extracted-netlist file -- the "
                "LVS deck script likely failed before completing "
                "extraction. klayout's own output:\n"
                + (completed.stdout or completed.stderr or "").strip()
            )

        import klayout.db as kdb

        parsed = kdb.Netlist()
        reader = kdb.NetlistSpiceReader()
        try:
            parsed.read(netlist_path, reader)
        except Exception as exc:
            raise ExtractError(
                f"could not parse native-deck extracted netlist '{netlist_path}': {exc}"
            ) from exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    devices: list[dict[str, Any]] = []
    device_counts: dict[str, int] = {}
    circuit = parsed.circuit_by_name(top_cell_name)
    if circuit is not None:
        for device in circuit.each_device():
            device_class = device.device_class()
            class_name = device_class.name
            params: dict[str, float] = {}
            for param_def in device_class.parameter_definitions():
                raw = device.parameter(param_def.name)
                if bare_length_area_units:
                    if param_def.name in _NATIVE_LVS_LENGTH_PARAMS:
                        raw /= 1.0e6
                    elif param_def.name in _NATIVE_LVS_AREA_PARAMS:
                        raw /= 1.0e12
                params[param_def.name] = raw
            devices.append(
                {
                    "name": device.expanded_name(),
                    "class": class_name,
                    "params": params,
                }
            )
            device_counts[class_name] = device_counts.get(class_name, 0) + 1

    devices.sort(key=lambda d: d["name"])

    return {
        "schema_version": 1,
        "file": path,
        "deck": lvs_deck_file,
        "engine": "klayout",
        "top": top_cell_name,
        "device_count": len(devices),
        "device_counts": dict(sorted(device_counts.items())),
        "devices": devices,
    }


def _label_layer_strings(
    layout: kdb.Layout,
    cell: kdb.Cell,
    layers: list[tuple[int, int] | None],
    *,
    recursive: bool,
) -> set[str]:
    """The set of text strings on ``layers`` under ``cell`` (issue #291).

    ``recursive=False`` reads only shapes drawn *directly* in ``cell``
    (``cell.shapes``); ``recursive=True`` reads the whole sub-tree
    (``begin_shapes_rec``, the same flatten :func:`_texts` uses). The
    difference -- strings that appear recursively but not directly in the top
    cell -- is exactly the set of labels that live below an instance boundary,
    i.e. sub-cell port names that are internal nodes once instanced.

    ``None`` layers (a deck that declares no such label layer) and layers
    absent from the stream contribute nothing.
    """
    import klayout.db as kdb

    strings: set[str] = set()
    for layer in layers:
        if layer is None:
            continue
        layer_index = layout.find_layer(*layer)
        if layer_index is None:
            continue
        shapes = (
            cell.begin_shapes_rec(layer_index)
            if recursive
            else cell.shapes(layer_index)
        )
        for text in kdb.Texts(shapes).each():
            strings.add(text.string)
    return strings


def _reconcile_top_pins(
    netlist: kdb.Netlist,
    top_name: str,
    below_top_labels: set[str],
    *,
    demote: bool,
) -> list[str]:
    """Reconcile the top circuit's pins against ``below_top_labels`` (issue
    #291), the label strings that name a net only from below an instance
    boundary.

    ``make_top_level_pins()`` has already promoted every named net. This finds
    the promoted pins whose net name is a below-top label and, when
    ``demote`` is set, removes those pins (the net keeps its name and stays an
    internal node). Returns the sorted, de-duplicated net names affected --
    the input to the caller's ``warnings`` entry, whether or not they were
    actually demoted.

    Global/substrate nets (named by ``connect_global``, not by any drawn text)
    are never in ``below_top_labels``, so a substrate pin is left untouched.
    """
    circuit = netlist.circuit_by_name(top_name)
    if circuit is None or not below_top_labels:
        return []

    affected: set[str] = set()
    to_remove: list[int] = []
    for pin in circuit.each_pin():
        net = circuit.net_for_pin(pin.id())
        if net is None:
            continue
        name = net.name
        if name and name in below_top_labels:
            affected.add(name)
            if demote:
                to_remove.append(pin.id())

    for pin_id in to_remove:
        circuit.remove_pin(pin_id)

    return sorted(affected)


def _promote_orphan_named_nets(netlist: kdb.Netlist) -> None:
    """Promote to pins the top-circuit named nets ``make_top_level_pins()``
    silently skips (issue #539).

    ``Netlist.make_top_level_pins()``'s docstring says it "will turn all
    named nets of top-level circuits ... into pins", but empirically
    (verified directly against ``klayout.db``, independent of this module)
    it only promotes a named net that already has at least one device
    terminal or subcircuit pin attached -- the same internal "not floating"
    test ``Net.is_floating()``/``purge()`` use. A net that is *only* named
    (e.g. a bond pad, seal ring, or RDL segment carrying a pin-purpose
    label but touching zero recognised devices) is skipped entirely, even
    though it is real, distinct, and correctly labelled.

    Called right after ``netlist.make_top_level_pins()`` and *before* the
    below-top-label/declared-pins demotion passes, so a net this function
    promotes is still subject to exactly the same demotion rules as a
    "normally" promoted one -- no separate code path for orphan nets to
    silently diverge from.
    """
    for circuit in netlist.top_circuits():
        for net in circuit.each_net():
            if not net.name:
                continue
            if net.pin_count() > 0:
                continue
            if net.terminal_count() or net.subcircuit_pin_count():
                continue
            pin = circuit.create_pin(net.name)
            circuit.connect_pin(pin, net)


def _purge_preserving_named_nets(netlist: kdb.Netlist) -> None:
    """Run ``Netlist.purge()`` without discarding named/labelled nets that
    are currently exposed as a pin but have no device terminal and no
    subcircuit pin attached (issue #539).

    ``Netlist.purge()``'s own floating-net definition -- "no device and no
    subcircuit on it" -- does *not* treat a pin connection as exempting a
    net: verified directly against ``klayout.db`` that manually creating
    and connecting a pin to such a net and then calling ``purge()`` still
    removes the net *and* its pin, and (when nothing else keeps the owning
    circuit alive) the circuit itself. A circuit whose nets are only ever
    named/labelled -- zero recognised devices -- would otherwise purge to
    an empty netlist even though its nets are real, distinct, and
    correctly labelled (the bug this issue reports).

    Snapshots every currently-*pinned* net that would otherwise be treated
    as floating -- name, owning circuit -- *before* calling the real
    ``purge()``, so every other case is still cleaned up exactly as
    before: genuinely floating unnamed junk nets, genuinely empty
    circuits, *and* a named-but-never-promoted internal net (e.g. one the
    below-top-label/declared-pins reconciliation passes deliberately left
    unpinned) -- the latter has no SPICE representation at all (no pin, no
    element references it), so preserving it in the in-memory netlist
    would silently diverge from what a round trip through
    ``NetlistSpiceWriter``/``NetlistSpiceReader`` (as `klt lvs`'s
    pre-extracted-reference path does) can actually reproduce. Restricting
    this guard to pinned nets keeps every consumer's view consistent.

    Restores whatever ``purge()`` removed from the pinned survivors:
    recreating the owning circuit if it was dropped entirely, the net if
    it was dropped, and the pin (reconnected to the recreated net).

    A recreated circuit/net also carries its predecessor's
    ``Circuit.cell_index``/``Net.cluster_id`` across the purge. That pair is
    the key ``LayoutToNetlist`` uses to find a net's shapes, so a rescued net
    stays fully queryable -- ``polygons_of_net`` (and therefore
    :func:`_compute_parasitics`, which runs *after* this pass and iterates
    every surviving net) returns its real geometry instead of faulting inside
    KLayout's hierarchical network processor on the default cluster id ``0``.
    Without it, ``klt extract --parasitics`` crashed with an unhandled
    internal ``RuntimeError`` on exactly the device-less labelled layouts this
    function exists to preserve (bond pads, seal rings, RDL segments,
    power-mesh straps -- all plausible parasitic-extraction targets).

    A circuit with genuinely no devices *and* no named/labelled nets finds
    no survivors to snapshot here, so it purges to nothing exactly as
    before -- this function only ever *adds back* pinned nets ``purge()``
    would otherwise silently drop, never changes behaviour for the
    legitimate "nothing extracted" case.
    """
    import klayout.db as kdb

    survivors: list[tuple[str, int, str, int]] = []
    for circuit in netlist.each_circuit():
        for net in circuit.each_net():
            if not net.name or net.pin_count() == 0:
                continue
            if net.terminal_count() or net.subcircuit_pin_count():
                continue
            survivors.append(
                (circuit.name, circuit.cell_index, net.name, net.cluster_id)
            )

    netlist.purge()

    for circuit_name, cell_index, net_name, cluster_id in survivors:
        circuit = netlist.circuit_by_name(circuit_name)
        if circuit is None:
            circuit = kdb.Circuit()
            circuit.name = circuit_name
            # Re-link the recreated circuit to the layout cell it was
            # extracted from (issue #563): `LayoutToNetlist`'s shape queries
            # (`polygons_of_net`, used by `_compute_parasitics`) look the
            # net's cluster up in the per-cell cluster store keyed by
            # `Circuit.cell_index`. A bare `kdb.Circuit()` leaves that at its
            # default, so every later shape query against a net it owns would
            # fault inside KLayout's hierarchical network processor.
            circuit.cell_index = cell_index
            netlist.add(circuit)

        net = next((n for n in circuit.each_net() if n.name == net_name), None)
        if net is None:
            net = circuit.create_net(net_name)
            # Same rationale as `cell_index` above, for the other half of the
            # (cell, cluster) key: `Net.cluster_id` is what ties a net back to
            # the connectivity cluster `LayoutToNetlist` extracted it from. A
            # freshly created net starts at cluster 0 -- the sentinel KLayout
            # asserts against (`id > 0 was not true in
            # LayoutToNetlist.polygons_of_net`) -- so a rescued net must carry
            # the id its purged predecessor had, or `klt extract --parasitics`
            # crashes on exactly the device-less labelled layouts (bond pads,
            # seal rings, RDL, power straps) issue #539 exists to preserve.
            net.cluster_id = cluster_id

        if net.pin_count() == 0:
            pin = circuit.create_pin(net_name)
            circuit.connect_pin(pin, net)


def _purge_truly_floating_nets(netlist: kdb.Netlist) -> None:
    """Remove every net, on every circuit, that has **no** pin, **no**
    device terminal, and **no** subcircuit pin -- and nothing else (issue
    #620's ``--abstract-cells`` purge path).

    Unlike ``Netlist.purge()`` (and the ``_purge_preserving_named_nets``
    rescue built on top of it), this never removes a circuit or a
    ``SubCircuit`` instance, and never removes a net that carries *any*
    connection at all, regardless of whether that connection eventually
    leads to a real ``kdb.Device`` anywhere in the hierarchy. See the call
    site's comment (in :func:`_extract_netlist`) for why that distinction
    matters once a black-box abstraction -- which is, by definition,
    device-free -- is in the netlist: ``Netlist.purge()`` judges an entire
    subcircuit chain "unused" (and deletes the circuit, the ``SubCircuit``
    instance, and the parent net it was wired to) whenever that chain is not
    transitively connected to a real device, which is *always* true for a
    pure black-box instance.
    """
    for circuit in netlist.each_circuit():
        floating = [
            net
            for net in circuit.each_net()
            if net.pin_count() == 0
            and net.terminal_count() == 0
            and net.subcircuit_pin_count() == 0
        ]
        for net in floating:
            circuit.remove_net(net)


def _resolve_black_box_regions(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    active: kdb.Region,
    poly: kdb.Region,
    nwell: kdb.Region,
    tap: kdb.Region,
    contact: kdb.Region,
    metals: list[kdb.Region],
    vias: list[kdb.Region],
    well_label: kdb.Texts,
    poly_label: kdb.Texts,
    metal_labels: list[kdb.Texts],
) -> tuple[
    list[dict[str, Any]],
    kdb.Region,
    kdb.Region,
    kdb.Region,
    kdb.Region,
    kdb.Region,
    list[kdb.Region],
    list[kdb.Region],
    kdb.Texts,
    kdb.Texts,
    list[kdb.Texts],
]:
    """Resolve black-box/abstract regions (issue #293) against this layout.

    A shape drawn on any reserved annotation layer (990-999, any datatype --
    see :func:`is_reserved_annotation_layer` in ``_annotation.py``) directly
    under ``top_cell`` marks a region whose contents are deliberately out of
    scope for connectivity: a sub-cell that will be drawn later (its
    hierarchy/area needs to be recorded now, its content doesn't yet), or a
    drawn region that is deliberately out of scope for a compare. Everything
    geometrically inside such a marker shape is excluded from the
    connectivity graph *before* any device extractor runs -- the same "cut a
    hole in the conductor region" shape :func:`_resolve_resistors` below
    uses for a single conductor layer, generalised here to every conductor/
    label layer this deck's connectivity graph reads.

    Returns ``(black_box_regions, active, poly, nwell, tap, contact, metals,
    vias, well_label, poly_label, metal_labels)`` where ``black_box_regions``
    is the JSON response's new field (one entry per geometrically separate
    marker shape -- non-touching marker shapes are reported individually,
    never merged into one bbox) and the remaining values are the caller's
    originals with every black-box region **subtracted**. A layout with no
    reserved-layer geometry returns the inputs unchanged and an empty list,
    so extraction of a layout that never uses this feature is bit-for-bit
    what it was before this feature existed.

    ``black_box_regions[].shapes_excluded`` counts, per marked region, the
    conductor/label shapes that actually overlap it (summed across every
    layer subtracted below) -- the signal that the exclusion did something,
    not just that a marker shape exists somewhere in the stream.

    The marker layer itself is never registered with ``l2n`` (nothing else in
    this module calls :func:`_region`/:func:`_texts` for a reserved layer),
    so it stays absent from ``ignored_layers`` exactly as it was before this
    feature existed -- see ``docs/cli/extract.md``'s "Reserved annotation
    layer".
    """
    import klayout.db as kdb

    marker = kdb.Region()
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        if is_reserved_annotation_layer(info.layer, info.datatype):
            marker += kdb.Region(top_cell.begin_shapes_rec(layer_index))
    marker = marker.merged()

    if marker.is_empty():
        return (
            [],
            active,
            poly,
            nwell,
            tap,
            contact,
            metals,
            vias,
            well_label,
            poly_label,
            metal_labels,
        )

    dbu = layout.dbu
    conductor_regions = [active, poly, nwell, tap, contact, *metals, *vias]
    label_collections = [well_label, poly_label, *metal_labels]

    black_box_regions: list[dict[str, Any]] = []
    for component in marker.each():
        component_region = kdb.Region(component)
        shapes_excluded = sum(
            region.interacting(component_region).count() for region in conductor_regions
        ) + sum(
            texts.interacting(component_region).count() for texts in label_collections
        )
        box = component.bbox()
        black_box_regions.append(
            {
                "bbox_um": _bbox_um_rounded(box, dbu),
                "shapes_excluded": shapes_excluded,
            }
        )

    black_box_regions.sort(
        key=lambda entry: (entry["bbox_um"]["left"], entry["bbox_um"]["bottom"])
    )

    return (
        black_box_regions,
        active - marker,
        poly - marker,
        nwell - marker,
        tap - marker,
        contact - marker,
        [region - marker for region in metals],
        [region - marker for region in vias],
        well_label.not_interacting(marker),
        poly_label.not_interacting(marker),
        [texts.not_interacting(marker) for texts in metal_labels],
    )


# --------------------------------------------------------------------------- #
# Cell-level (black-box + pins) abstraction -- `--abstract-cells`, issue #620.
# Moved to `extract_abstract.py` (issue #1303); the handful of names this
# module still calls directly are re-imported below.
# --------------------------------------------------------------------------- #


def _resolve_resistors(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    poly: kdb.Region,
    active: kdb.Region,
    metals: list[kdb.Region],
    dummy: kdb.Region,
) -> tuple[
    list[tuple[ResistorDevice, kdb.Region, kdb.Region]],
    kdb.Region,
    kdb.Region,
    list[kdb.Region],
    kdb.Region,
    int,
]:
    """Resolve the deck's drawn-resistor declarations against this layout.

    Returns ``(resistors, poly, active, metals, poly_candidate_bodies,
    dummy_devices_dropped)`` where ``resistors`` is one ``(spec,
    body_region, terminal_region)`` triple per *recognised* device class and
    the three conductor regions are the deck's originals with every
    recognised resistor body **subtracted** -- so the caller's connectivity
    graph (and its MOS gate/source-drain split) sees the resistor's heads as
    ordinary conductor and the resistive segment as a hole, instead of one
    continuous short (see :class:`~klayout_tools.decks.ResistorDevice`).

    A resistor body is ``body_layer & marker & all(requires) - any(excludes)``.
    A spec whose body region comes out empty on this layout (the common case
    -- no PDK resistor marker drawn anywhere) is dropped entirely and
    subtracts nothing, so extraction of a resistor-free layout is bit-for-bit
    what it was before this feature existed.

    ``dummy`` is the deck's optional dummy-device marker region (see
    ``ExtractionDeck.dummy``, issue #295) -- possibly empty when the deck
    declares no ``dummy`` layer or the layout draws no such geometry.
    Mirroring the MOS gate-suppression idiom in ``_extract_netlist``, a
    resistor body's *candidate* region (after ``requires``/``excludes`` but
    before the ``dummy`` cut) has ``dummy`` subtracted before it is handed
    off as a recognised device: any *connected component* of the candidate
    fully consumed by ``dummy`` is dropped outright (counted into
    ``dummy_devices_dropped``, issue #462) rather than registered as a
    device, while a component only partially covered survives as a clean
    geometric cut -- the same all-or-nothing distinction the MOS path
    already makes. Whatever ``dummy`` removes from a candidate body is *not*
    subtracted from the caller's conductor region, so it stays present as
    ordinary conductor, exactly like a suppressed MOS gate's poly.

    ``poly_candidate_bodies`` is the union of every *candidate* body region
    (post ``requires``/``excludes``, but **before** the ``dummy`` cut) whose
    ``spec.body`` is the deck's ``poly`` layer -- used by
    :func:`_detect_unmodelled_poly_bodies` to recognise a fully
    dummy-suppressed poly resistor's own footprint as "claimed" (so it is
    never misflagged as an unmodelled-device gap, issue #462), distinct from
    the narrower post-dummy body carried in ``resistors`` itself.

    Raises :class:`ExtractError` for a deck-authoring mistake (a ``body``/
    ``terminal`` layer that is not one of the deck's own conductor layers),
    since the terminal region must be a layer the connectivity graph already
    carries.
    """
    import klayout.db as kdb

    if not deck.resistors:
        return [], poly, active, metals, kdb.Region(), 0

    # Keyed by drawn conductor layer so a resistor declared on `poly` is cut
    # out of the very same Region the connectivity graph uses.
    bases: dict[tuple[int, int], kdb.Region] = {deck.poly: poly, deck.active: active}
    for index, layer in enumerate(deck.metals):
        bases.setdefault(layer, metals[index])

    def _conductor(layer: tuple[int, int], field: str, name: str) -> kdb.Region:
        try:
            return bases[layer]
        except KeyError:
            raise ExtractError(
                f"resistor '{name}': {field} layer {layer[0]}/{layer[1]} is not one "
                "of the deck's conductor layers (active/poly/metals)"
            ) from None

    recognised: list[tuple[ResistorDevice, kdb.Region, tuple[int, int]]] = []
    poly_candidate_bodies = kdb.Region()
    dummy_devices_dropped = 0
    for spec in deck.resistors:
        base = _conductor(spec.body, "body", spec.name)
        terminal_layer = spec.terminal if spec.terminal is not None else spec.body
        _conductor(terminal_layer, "terminal", spec.name)

        body = base & _region(layout, top_cell, spec.marker)
        for layer in spec.requires:
            body = body & _region(layout, top_cell, layer)
        for layer in spec.excludes:
            body = body - _region(layout, top_cell, layer)
        if body.is_empty():
            continue
        if spec.body == deck.poly:
            poly_candidate_bodies += body
        if not dummy.is_empty():
            for component in body.merged().each():
                if (kdb.Region(component) - dummy).is_empty():
                    dummy_devices_dropped += 1
            body = body - dummy
        if body.is_empty():
            continue
        recognised.append((spec, body, terminal_layer))

    for spec, body, _terminal_layer in recognised:
        bases[spec.body] = bases[spec.body] - body

    resistors = [
        (spec, body, bases[terminal_layer]) for spec, body, terminal_layer in recognised
    ]
    return (
        resistors,
        bases[deck.poly],
        bases[deck.active],
        [bases[layer] for layer in deck.metals],
        poly_candidate_bodies,
        dummy_devices_dropped,
    )


def _capacitor_plate_regions(
    layout: kdb.Layout, top_cell: kdb.Cell, capacitor: CapacitorDevice
) -> tuple[kdb.Region, kdb.Region]:
    """The recognised ``(top_region, bottom_region)`` pair for one
    :class:`CapacitorDevice` entry against this layout.

    Shared between the main capacitor-recognition loop in
    ``_extract_netlist`` and :func:`_exclude_capacitor_top_via_overlap`
    below (issue #364), so the two never drift apart on what counts as
    "this capacitor's bottom plate" -- the overlap exclusion must be
    computed against exactly the same (possibly virtual-bottom-plate-
    clipped, requires/excludes-narrowed) region the capacitor device itself
    is later registered and extracted against.

    Either region comes back empty when the capacitor's markers are not
    drawn anywhere on this layout (the common case -- no PDK cap marker
    drawn at all).
    """
    top_region = _region(layout, top_cell, capacitor.top_plate)
    for layer in capacitor.top_plate_requires:
        top_region = top_region & _region(layout, top_cell, layer)
    for layer in capacitor.top_plate_excludes:
        top_region = top_region - _region(layout, top_cell, layer)

    bottom_conductor = _region(layout, top_cell, capacitor.bottom_plate)
    for layer in capacitor.bottom_plate_requires:
        bottom_conductor = bottom_conductor & _region(layout, top_cell, layer)
    for layer in capacitor.bottom_plate_excludes:
        bottom_conductor = bottom_conductor - _region(layout, top_cell, layer)

    if capacitor.bottom_plate_oversize_um:
        # "Virtual bottom plate" derivation (e.g. gf180mcu's MiM stack): only
        # bottom-conductor shapes that already touch the *unsized* top plate
        # count (`interacting`), then clipped to the top plate's oversized
        # outline for the exact overlap area -- the same two-step derivation
        # the PDK's own official KLayout LVS deck uses (see
        # `CapacitorDevice`'s docstring).
        oversize_dbu = int(round(capacitor.bottom_plate_oversize_um / layout.dbu))
        bottom_region = bottom_conductor.interacting(top_region) & (
            top_region.sized(oversize_dbu)
        )
    else:
        bottom_region = bottom_conductor

    return top_region, bottom_region


def _diode_terminal_region(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    marker: kdb.Region,
    layer: tuple[int, int] | None,
    requires: tuple[tuple[int, int], ...],
    excludes: tuple[tuple[int, int], ...],
) -> kdb.Region:
    """The recognised region for **one** terminal of a :class:`DiodeDevice`
    entry (issue #542), against this layout.

    ``layer`` is the terminal's declared drawn layer, scoped to the device's
    already-built ``marker`` region -- the same "intersect with the device
    mark before recognition" guard the bipolar block applies to its base, so
    an ordinary PMOS's p+-in-Nwell diffusion is never misrecognised as a
    diode. ``None`` means the terminal is formed by the substrate (the PDK
    draws no mask for it): the region is then the device's ``marker``
    footprint itself, which the caller ties to the deck's ``substrate_net``
    global. That footprint -- rather than an empty region -- is load-bearing:
    ``kdb.DeviceExtractorDiode`` forms the device from the *overlap* of its
    two inputs, so an empty input silently yields no device at all.

    ``requires``/``excludes`` then narrow the result the same way
    :func:`_capacitor_plate_regions` and ``_resolve_resistors`` narrow
    theirs: every ``requires`` layer must also cover the region, every
    ``excludes`` layer is subtracted. For a substrate-formed terminal this
    is how the deck keeps the substrate side genuinely *outside* every well
    (``anode_excludes=(Nwell, DNWELL)``).

    Always returns a freshly-owned ``Region``: both terminals of the same
    entry can derive from the one ``marker`` object, and each is registered
    into the connectivity graph separately.
    """
    # `&`/`-` below already return fresh regions; the `dup()` branch covers
    # the no-declared-layer, no-narrowing case.
    if layer is None:
        region = marker.dup()
    else:
        region = _region(layout, top_cell, layer) & marker
    for required in requires:
        region = region & _region(layout, top_cell, required)
    for excluded in excludes:
        region = region - _region(layout, top_cell, excluded)
    return region


def _exclude_capacitor_top_via_overlap(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    vias: list[kdb.Region],
) -> list[kdb.Region]:
    """Exclude each capacitor's own ``top_plate_via ∩ bottom_plate`` overlap
    from the deck's generic ``vias[]`` layers (issue #364), before those
    layers are registered into the connectivity graph and consumed by
    ``_extract_netlist``'s generic per-layer ``metals[i]``/``vias[i]`` loop.

    A capacitor's ``top_plate_via`` (#314) is wired directly to the
    recognised top-plate region and on to ``top_plate_via_metal`` -- but
    without this exclusion, that *same* via shape also reaches the deck's
    generic per-layer connectivity loop, which connects every ``vias[i]``
    shape to whichever ``metals[i]``/``metals[i + 1]`` conductor it
    geometrically touches. A top-plate via placed per the PDK's own
    minimum-overlap rule for that via (which *requires* the bottom plate to
    enclose/overlap it, not clear it) inevitably touches the bottom-plate
    conductor beneath it in plan view, so the generic loop reads that
    DRM-legal overlap as an ordinary via shorting the two plates together --
    a false short (the extraction engine has no notion of the dielectric
    that keeps the via from actually reaching the bottom plate in real
    silicon).

    Only the *geometric intersection* of the via footprint with the
    capacitor's own recognised bottom-plate region is cut, not the whole via
    shape/component: a via landing pad that only partially overlaps the
    bottom plate keeps the rest of its footprint in the generic connectivity
    graph, and any *other* via shape drawn on the same physical via layer
    elsewhere in the layout -- ordinary routing unrelated to this capacitor
    -- is left untouched. That last guarantee requires narrowing
    ``bottom_region`` to ``interacting(top_region)`` before intersecting it
    with the via footprint (issue #1388): for a deck with
    ``bottom_plate_oversize_um == 0`` (e.g. sky130's MiM stacks),
    :func:`_capacitor_plate_regions` hands back the bottom conductor's
    *entire* drawn region on that metal, not just the part under this
    capacitor's own top plate, so without the narrowing a single drawn
    capacitor would exclude every via on the shared via layer chip-wide --
    including ordinary routing vias nowhere near a capacitor.

    Returns a new ``vias`` list (the input list/regions are not mutated); a
    deck with no capacitor declaring ``top_plate_via``, or whose declared
    ``top_plate_via`` is not one of the deck's own ``vias`` layers (so it
    never reaches the generic loop in the first place), returns the input
    list unchanged.
    """
    import klayout.db as kdb

    exclusions: dict[int, kdb.Region] = {}
    for capacitor in deck.capacitors:
        if capacitor.top_plate_via is None:
            continue
        if capacitor.top_plate_via not in deck.vias:
            # Not one of this deck's tracked via layers -- the generic loop
            # below never touches it, so there is nothing to exclude (the
            # deck-authoring validation for a mismatched
            # `top_plate_via`/`top_plate_via_metal` pair is the main
            # capacitor loop's job, not this helper's).
            continue
        via_index = deck.vias.index(capacitor.top_plate_via)
        top_via_region = _region(layout, top_cell, capacitor.top_plate_via)
        if top_via_region.is_empty():
            continue
        top_region, bottom_region = _capacitor_plate_regions(
            layout, top_cell, capacitor
        )
        # For a deck whose `bottom_plate` is *not* clipped to the top
        # plate's own footprint (`bottom_plate_oversize_um == 0`, e.g.
        # sky130's MiM stacks), `_capacitor_plate_regions`'s zero-oversize
        # branch returns the bottom conductor's *entire* drawn region --
        # every shape on that metal layer anywhere in the layout, not just
        # this capacitor's own plate. Narrowing to `interacting(top_region)`
        # here (issue #1388) keeps only the bottom-plate shape(s) that
        # actually sit under *this* capacitor's top-plate marker, the same
        # scoping the nonzero-oversize branch above already applies when it
        # derives `bottom_region` itself. This both restores the issue #775
        # guard (an empty `top_region` -- no cap marker drawn anywhere --
        # makes `scoped_bottom_region` empty too, so a digital/macro layout
        # that only routes on the declared `bottom_plate` metal is
        # untouched) *and* fixes the case #775 didn't cover: a layout that
        # draws both a real capacitor and ordinary routing between the
        # bottom-plate metal and the metal above elsewhere on the chip.
        # Without this narrowing, `top_via_region` (every shape on the
        # declared `top_plate_via` layer, e.g. sky130's real `via3`/`via4`
        # routing vias used throughout ordinary signal routing) intersected
        # against the unscoped, chip-wide `bottom_region` excludes every
        # legitimate via on that layer from the deck's generic `vias[]`
        # connectivity -- a false disconnect across the whole design, not
        # the narrow false-short exclusion this function exists to apply.
        scoped_bottom_region = bottom_region.interacting(top_region)
        if top_region.is_empty() or scoped_bottom_region.is_empty():
            continue
        overlap = top_via_region & scoped_bottom_region
        if overlap.is_empty():
            continue
        exclusions[via_index] = exclusions.get(via_index, kdb.Region()) + overlap

    if not exclusions:
        return vias
    return [
        region - exclusions[index] if index in exclusions else region
        for index, region in enumerate(vias)
    ]


#: Minimum number of geometrically separate `contact` clusters a candidate
#: poly component must touch to be flagged by `_detect_unmodelled_poly_bodies`
#: -- the "resistor-body signature": a two-terminal conductor segment
#: contacted at *each* end, rather than routing with a single landing pad.
_UNMODELLED_POLY_MIN_CONTACT_CLUSTERS = 2


def _detect_unmodelled_poly_bodies(
    poly: kdb.Region,
    contact: kdb.Region,
    nfet_gate: kdb.Region,
    pfet_gate: kdb.Region,
    resistor_markers: kdb.Region | None = None,
    resistor_bodies: kdb.Region | None = None,
    dbu: float = 1.0,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Flag ``poly`` connected components that no MOS gate extractor claims
    and that touch ``contact`` at two or more geometrically separate
    locations -- the resistor-body signature (issue #288).

    This deck's device extractors are a fixed, curated subset (today:
    ``nfet``/``pfet``, an optional declared drawn resistor/bipolar/
    capacitor). Geometry drawn for any device class the deck does not
    (yet) recognise is still built out of ordinary connectivity layers
    (``poly``, ``contact``, ...), so :func:`_extract_netlist`'s blanket
    ``l2n.connect(poly, contact)`` (and friends) absorbs it into ordinary
    interconnect -- silently shorting two terminals a schematic keeps
    distinct, with zero signal in ``warnings`` today. This is a narrowly
    scoped *diagnostic* heuristic, not a device extractor: it identifies
    the shape, not the missing device class, and is deliberately
    conservative to avoid false positives on real layouts (see
    ``docs/cli/extract.md``'s "Known limitation: unmodelled device
    geometry").

    Called with ``poly`` as it stands *after* :func:`_resolve_resistors`
    has already subtracted out every resistor body this deck's own
    ``ResistorDevice`` declarations recognise -- so a properly marked
    drawn resistor never reaches this heuristic at all. What is left is
    only genuinely unrecognised geometry: a resistor drawn without (or
    excluded from) the deck's marker layer, or any other not-yet-modelled
    device class that happens to share poly + contact.

    A component is skipped unconditionally if it touches ``nfet_gate``/
    ``pfet_gate`` anywhere -- a real MOS gate (with or without a
    legitimate poly-contacted gate strap) or ordinary poly routing between
    two recognised gates (poly needs no via to route on itself, so both
    ends of such a run are one merged polygon together with the gates
    themselves), never a candidate unmodelled-device body. It is likewise
    skipped unconditionally if it touches ``resistor_bodies`` (issue #324) --
    the recognised body regions ``_resolve_resistors`` already cut out of
    ``poly`` -- since a poly component *abutting* one of those bodies is that
    resistor's own terminal head, by construction: the head survives as a
    separate connected component once its body is subtracted, and legitimately
    carries a normal (2+) contact array with no device-recognition gap behind
    it at all.

    ``resistor_markers`` (issue #299) is the union of every ``ResistorDevice``
    marker layer this deck declares on ``poly`` (regardless of any
    ``requires``/``excludes`` narrowing) -- the raw resistor-ID geometry, not
    the narrower recognised-body region ``_resolve_resistors`` already cut
    out. It distinguishes two different reasons a flagged component reaches
    this heuristic at all: a component overlapping it carries a resistor
    marker this deck *knows about* but whose ``requires``/``excludes``
    conditions this specific segment did not satisfy (a **deck-coverage
    gap** -- e.g. gf180mcu's ``RES_MK`` present without the ``SAB``/``Pplus``
    combination any declared entry needs), versus a component that carries
    none of this deck's resistor markers at all (an **unmarked** shape, the
    original #288 case -- some other, entirely undeclared device class, or a
    resistor with no marker drawn). ``None`` (the default) treats every
    flagged component as unmarked, matching this function's behaviour before
    #299.

    ``resistor_bodies`` (issue #324) is the union of every *recognised*
    ``ResistorDevice`` body region ``_resolve_resistors`` returned for this
    layout (already narrowed by each entry's own ``requires``/``excludes``) --
    distinct from ``resistor_markers`` above, which is the raw, unnarrowed
    marker geometry. ``None`` (the default) skips no component on this basis,
    matching this function's behaviour before #324.

    ``dbu`` converts each flagged component's bounding box from database
    units to micrometres for ``unmodelled_poly[]`` (see below); defaults to
    ``1.0`` (i.e. no conversion) for callers that only need the warning
    strings.

    Returns ``(warnings, unmodelled_poly)``. ``warnings`` has up to two
    strings (empty when nothing is flagged) -- at most one for unmarked
    shapes and one for marked-but-unrecognised ones -- each naming how many
    components were flagged and pointing at the documented limitation rather
    than guessing a device name. ``unmodelled_poly`` (issue #324) is one
    entry per flagged component -- ``{"bbox_um": {"left", "bottom", "right",
    "top"}, "reason": "unmarked" | "marked_unrecognised"}`` -- sorted by
    ``(left, bottom)`` for deterministic output, so a consumer can enumerate
    and triage the exact flagged shapes instead of re-deriving them by
    re-implementing this heuristic against the stream. Always a list, empty
    when ``warnings`` is empty.
    """
    import klayout.db as kdb

    gate_regions = nfet_gate + pfet_gate
    bodies = resistor_bodies if resistor_bodies is not None else kdb.Region()
    markers = resistor_markers if resistor_markers is not None else kdb.Region()
    unmarked = 0
    marked_unrecognised = 0
    unmodelled_poly: list[dict[str, Any]] = []
    for component in poly.merged().each():
        candidate = kdb.Region(component)
        if not candidate.interacting(gate_regions).is_empty():
            continue
        if not candidate.interacting(bodies).is_empty():
            continue
        contact_clusters = (candidate & contact).merged().count()
        if contact_clusters < _UNMODELLED_POLY_MIN_CONTACT_CLUSTERS:
            continue
        if candidate.interacting(markers).is_empty():
            unmarked += 1
            reason = "unmarked"
        else:
            marked_unrecognised += 1
            reason = "marked_unrecognised"
        box = component.bbox()
        unmodelled_poly.append(
            {
                "bbox_um": _bbox_um_rounded(box, dbu),
                "reason": reason,
            }
        )

    unmodelled_poly.sort(
        key=lambda entry: (entry["bbox_um"]["left"], entry["bbox_um"]["bottom"])
    )

    warnings: list[str] = []
    if unmarked:
        shape_word = "shape" if unmarked == 1 else "shapes"
        warnings.append(
            f"{unmarked} poly-layer {shape_word} not part of any recognised nfet/pfet "
            "gate touch contact at 2+ separate points (the resistor-body "
            "signature) and carry no resistor-marker layer at all; this deck "
            "may not model the device class drawn here, and its terminals "
            "have been absorbed into ordinary interconnect as an unintended "
            "short -- see docs/cli/extract.md's 'Known limitation: unmodelled "
            "device geometry'."
        )
    if marked_unrecognised:
        shape_word = "shape" if marked_unrecognised == 1 else "shapes"
        warnings.append(
            f"{marked_unrecognised} poly-layer {shape_word} not part of any "
            "recognised nfet/pfet gate touch contact at 2+ separate points "
            "(the resistor-body signature) and carry a resistor-marker layer, "
            "but do not match any of this deck's declared ResistorDevice "
            "requires/excludes conditions (a deck-coverage gap, not unmarked "
            "geometry); their terminals have been absorbed into ordinary "
            "interconnect as an unintended short -- see docs/cli/extract.md's "
            "'Known limitation: unmodelled device geometry'."
        )
    return warnings, unmodelled_poly


def _detect_voltage_domain_overlap(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    deck_name: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Flag extracted MOS device geometry (``deck.active``) drawn inside a
    voltage-domain marker layer this deck does not model the scoping of
    (issue #552).

    Mirrors :func:`_detect_unmodelled_poly_bodies`'s "device carries a
    marker this deck doesn't model" shape, but for a *different* gap: not a
    device class this deck fails to recognise at all, but a device class
    (MOS) it recognises and extracts with the *wrong* model. Some decks
    (today, sg13g2's ``ThickGateOx`` 44/0, registered via
    :func:`~klayout_tools.decks.get_unmodeled_voltage_markers`) draw a
    marker selecting a second gate-oxide/voltage domain -- e.g. a thick-
    oxide flavour -- with its own correct MOS model, which this deck's
    ``ExtractionDeck.nfet_class``/``pfet_class`` derivation (well layer
    alone) does not read, so a transistor drawn entirely inside the marker
    still extracts bound to the default model name with no signal that
    anything is off.

    A marker this deck's ``ExtractionDeck.mos_flavours`` *also* declares
    (issue #1111 -- today, gf180mcu's ``Dualgate`` 55/0) is skipped
    entirely: MOS recognition for that marker is flavour-aware, so a
    transistor drawn inside it already extracts bound to the flavour's own
    real model (under ``--pdk``) -- the gap this function exists to flag
    does not apply to it, and flagging it anyway would be a stale warning
    about an already-closed gap.

    A marker/description pair is only flagged when the marker's geometry
    actually *interacts* with ``deck.active`` (the MOS device-recognition
    footprint, evaluated for the whole layout, both flavours combined) --
    not merely present somewhere in the stream -- so a marker shape drawn
    only over, say, an ESD diode this deck's ``DiodeDevice`` entries already
    scope correctly to it produces no false-positive warning here.

    Returns ``(warnings, voltage_domain_warnings)``: ``warnings`` has one
    prose string per flagged marker (empty when this deck registers no
    marker, every registered marker is covered by ``mos_flavours``, or none
    of the remainder overlaps ``deck.active``); ``voltage_domain_warnings``
    is the matching structured view -- one ``{"marker": "<layer>/<datatype>",
    "description": str}`` entry per flagged marker, mirroring ``klt drc``'s
    ``coverage.voltage_domain_warnings`` shape (same registry, same
    description text) so a caller correlating the two commands' output for
    the same layout sees the identical wording. Always a list, empty when
    nothing is flagged.
    """
    from .decks import get_unmodeled_voltage_markers

    unmodeled_markers = get_unmodeled_voltage_markers(deck_name)
    flavour_markers = {flavour.marker for flavour in deck.mos_flavours}
    unmodeled_markers = {
        marker: description
        for marker, description in unmodeled_markers.items()
        if marker not in flavour_markers
    }
    if not unmodeled_markers:
        return [], []

    active = _region(layout, top_cell, deck.active)
    if active.is_empty():
        return [], []

    warnings: list[str] = []
    voltage_domain_warnings: list[dict[str, Any]] = []
    for marker, description in sorted(unmodeled_markers.items()):
        marker_region = _region(layout, top_cell, marker)
        if marker_region.is_empty():
            continue
        if active.interacting(marker_region).is_empty():
            continue
        marker_label = f"{marker[0]}/{marker[1]}"
        warnings.append(
            f"MOS device geometry overlaps the '{marker_label}' "
            f"voltage-domain marker: {description}"
        )
        voltage_domain_warnings.append(
            {"marker": marker_label, "description": description}
        )
    return warnings, voltage_domain_warnings


def _partition_region_by_islands(
    region: kdb.Region, islands: list[kdb.Region]
) -> tuple[kdb.Region, list[kdb.Region]]:
    """Split ``region``'s own connected components into an "outside every
    island" remainder plus one bucket per entry in ``islands`` (issue #1128,
    see ``ExtractionDeck.substrate_isolation``'s docstring) -- the same "any
    overlap claims the whole connected component" idiom ``mos_flavours``
    marker classification already uses in :func:`_extract_netlist` (a
    component cannot legally straddle two distinct isolation islands any
    more than a transistor can legally straddle a voltage-domain marker
    boundary, so whichever island is tested first for an overlapping
    component wins).

    Returns ``(region, [])`` -- ``region`` itself, completely unmodified,
    with no connected-component walk at all -- when ``islands`` is empty.
    This is the overwhelmingly common case (a deck that declares no
    ``substrate_isolation``, or a layout that draws no shapes on one that is
    declared) and is deliberately a fast, no-op path: skipping the walk
    means the returned region is the exact same object passed in, not a
    reassembled-from-merged-components copy that could fracture polygons
    differently, so every caller stays byte-for-byte identical to the
    pre-#1128 code path in that case.
    """
    if not islands:
        return region, []
    import klayout.db as kdb

    outside = kdb.Region()
    per_island: list[kdb.Region] = [kdb.Region() for _ in islands]
    for component in region.merged().each():
        component_region = kdb.Region(component)
        for index, island in enumerate(islands):
            if not component_region.interacting(island).is_empty():
                per_island[index] += component_region
                break
        else:
            outside += component_region
    return outside, per_island


def _is_synthesized_substrate_net(name: str, deck: ExtractionDeck) -> bool:
    """Whether ``name`` is one of this deck's *synthesized* substrate
    identities rather than a real, drawn-and-labelled net name: the deck-wide
    ``substrate_net`` global itself, or one of the per-isolated-region
    ``f"{substrate_net}_iso{n}"`` identities issue #1128 mints (see
    ``ExtractionDeck.substrate_isolation``'s docstring).

    Neither is ever a name a layout can draw: ``connect_global`` invents them,
    so a net carrying one of these names is one that earned no label of its
    own anywhere in the layout.
    """
    return _is_synthesized_substrate_net_name(name, deck.substrate_net)


def _is_synthesized_substrate_net_name(name: str, substrate_net: str) -> bool:
    """:func:`_is_synthesized_substrate_net`'s predicate, expressed against a
    bare ``substrate_net`` string rather than a whole
    :class:`ExtractionDeck`.

    Split out for :func:`_tie_substrate_nets_to_ground` (issue #1263), which
    runs inside :func:`_inject_parasitics` -- a function that is handed the
    deck's ``substrate_net`` name (as ``ground_net_name``) but not the deck
    object. Keeping the one-line rule in a single place stops the two call
    sites drifting apart the next time the synthesized-identity naming
    convention grows a variant (it already grew ``_iso<n>`` once, issue
    #1128).
    """
    return name == substrate_net or name.startswith(f"{substrate_net}_iso")


def _region_probe_points(region: kdb.Region) -> list[kdb.Point]:
    """One point *strictly inside* each connected component of ``region``,
    suitable for ``LayoutToNetlist.probe_net``.

    A merged component's bbox centre is inside it for any convex (in
    practice: rectangular) shape, which is what a diffusion tap cut out of a
    device-mark footprint almost always is. A concave component (an L, a
    ring) is decomposed into rectilinear trapezoids first and one point per
    part is returned instead -- probing more points than strictly necessary
    is harmless (every part of one component resolves to the same net), while
    probing a point in the notch of an L would silently resolve to nothing.
    """
    import klayout.db as kdb

    points: list[kdb.Point] = []
    for polygon in region.merged().each():
        box = polygon.bbox()
        centre = kdb.Point((box.left + box.right) // 2, (box.bottom + box.top) // 2)
        if polygon.inside(centre):
            points.append(centre)
            continue
        for part in polygon.decompose_trapezoids():
            part_box = part.bbox()
            points.append(
                kdb.Point(
                    (part_box.left + part_box.right) // 2,
                    (part_box.bottom + part_box.top) // 2,
                )
            )
    return points


def _detect_diode_substrate_label_divergence(
    l2n: kdb.LayoutToNetlist,
    circuit: kdb.Circuit | None,
    deck: ExtractionDeck,
    diode_regions: list[tuple[DiodeDevice, kdb.Region, kdb.Region]],
    contact: kdb.Region,
    poly: kdb.Region,
    mos_source_drain: kdb.Region,
) -> list[str]:
    """Issue #1196: flag a diode whose *substrate-formed* terminal (declared
    ``None``, tied to the deck's synthesized ``substrate_net`` global by
    :func:`_extract_netlist`'s diode connectivity block) resolved to that
    synthesized name **while real, drawn, labelled tie geometry sits inside
    the very footprint that terminal is formed from**.

    Both halves of that condition matter:

    - A substrate-formed terminal usually *should* land on the synthesized
      global -- that is the documented fallback for a PDK that draws no
      p-substrate mask, and the common case for every gf180mcu corpus cell
      today. Warning on it unconditionally would be noise, so this never
      fires on a layout that draws no tie into the terminal's footprint at
      all.
    - When a tie *is* drawn and the deck's own substrate-tap derivation
      claims it (``tap``/``tap_nplus``/``tap_pplus``, issues #490/#1084),
      ``connect_global`` merges the two into one net and KLayout names the
      result from the drawn label -- so the terminal resolves to the real
      name and this never fires either (verified: a p+/Comp tap contacted up
      to a ``VSS``-labelled Metal1 gives the diode anode net ``VSS``, not
      ``vsubs``).

    What is left is exactly the silent case the issue reports: drawn,
    contacted, *labelled* geometry inside the terminal's footprint that the
    deck's tap derivation does **not** claim (an unimplanted diffusion tie, a
    tie the deck models as belonging to a different substrate identity, ...).
    Nothing joins the two, so the labelled net exists in the netlist beside
    the device while the terminal keeps the synthesized global -- previously
    with no trace anywhere in ``klt extract``'s output.

    The probe area is the deck's ``contact`` cuts landing inside the
    terminal's own footprint: a tie can only carry a *name* if it is
    contacted and routed up to a labelled conductor, and ``contact`` is one
    of the layers :func:`_extract_netlist` registers with ``l2n``, which is
    what ``probe_net`` requires (the deck's raw ``active`` region is not
    registered, and is in any case already split by well/gate/flavour by the
    time this runs). Subtracted from it first, so a *device's* own contacted
    terminal inside the same mark is never mistaken for a substrate tie:

    - every drawn (non-substrate-formed) diode terminal region of every
      entry -- e.g. the nd2ps cathode inside its own ``diode_mk`` footprint,
      and a sibling entry's terminals, since two entries may share one
      device-mark layer;
    - ``mos_source_drain``, the deck's MOS source/drain diffusion that
      actually *touches a gate* (i.e. belongs to a recognised transistor) --
      not the deck's whole ``active - poly`` split, which is every diffusion
      shape in the layout including a substrate tie;
    - ``poly``, so a poly contact inside the mark reports the gate's net
      rather than a tie.

    One aggregate warning per (diode entry, terminal), counting devices
    rather than listing them, mirroring this module's other aggregate
    ``warnings[]`` entries. Returns ``[]`` for every deck that declares no
    substrate-formed diode terminal at all.
    """
    import klayout.db as kdb

    if circuit is None:
        return []

    substrate_terminals = [
        (diode, terminal, region, sibling)
        for diode, anode_region, cathode_region in diode_regions
        for terminal, layer, region, sibling in (
            ("A", diode.anode, anode_region, cathode_region),
            ("C", diode.cathode, cathode_region, anode_region),
        )
        if layer is None
    ]
    if not substrate_terminals:
        return []

    # Every *drawn* diode terminal region, of every entry -- a diode's own
    # drawn terminal is a device terminal, never a substrate tie, and two
    # entries can share one device-mark layer (gf180mcu's two diodes both use
    # `diode_mk`), so a sibling entry's terminal can land inside this one's
    # footprint too. Substrate-formed (`None`-declared) terminals are
    # deliberately *not* collected: those regions are the device's own mark
    # footprint, which is exactly the area being probed here.
    drawn_terminals = kdb.Region()
    for diode, anode_region, cathode_region in diode_regions:
        if diode.anode is not None:
            drawn_terminals += anode_region
        if diode.cathode is not None:
            drawn_terminals += cathode_region

    warnings: list[str] = []
    for diode, terminal, region, sibling in substrate_terminals:
        unresolved = 0
        for device in circuit.each_device():
            device_class = device.device_class()
            if device_class.name != diode.name:
                continue
            for definition in device_class.terminal_definitions():
                if definition.name.upper() != terminal:
                    continue
                net = device.net_for_terminal(definition.id())
                if net is not None and _is_synthesized_substrate_net(
                    net.expanded_name(), deck
                ):
                    unresolved += 1
        if not unresolved:
            continue

        # `sibling` is subtracted explicitly as well as via `drawn_terminals`
        # to keep the intent readable: the other terminal of *this* junction
        # is the one most likely to sit inside this footprint.
        probe_region = (
            ((region & contact) - sibling) - drawn_terminals - mos_source_drain - poly
        )
        if probe_region.is_empty():
            continue

        labels: list[str] = []
        for point in _region_probe_points(probe_region):
            probed = l2n.probe_net(contact, point)
            if probed is None:
                continue
            name = probed.expanded_name()
            # `Net.name` is empty for a net that earned no drawn label, so
            # `expanded_name()` returns the anonymous `$n` spelling -- there
            # is no real name to have been discarded in that case. A label
            # spelled exactly like the deck's own synthesized substrate name
            # is likewise not a divergence (issue #1196's own no-false-
            # positive requirement).
            if not probed.name or _is_synthesized_substrate_net(name, deck):
                continue
            safe = spice_safe_net_name(name)
            if safe not in labels:
                labels.append(safe)
        if not labels:
            continue

        shown = ", ".join(sorted(labels)[:5])
        more = len(labels) - 5
        warnings.append(
            f"{unresolved} {diode.name} '{terminal.lower()}' terminal(s) "
            f"resolved to the deck-synthesized '{deck.substrate_net}' "
            "substrate net, but drawn, labelled tie geometry inside the same "
            f"device footprint resolves to a different net ({shown}"
            + (f", +{more} more" if more > 0 else "")
            + ") -- this deck's substrate-tap derivation does not claim that "
            "drawn shape, so the synthesized global was substituted for the "
            "drawn net name; check the tie's implant/tap layers (see "
            'docs/cli/extract.md, "Coverage")'
        )
    return warnings


def _extract_netlist(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    parasitics_deck: ParasiticsDeck | None = None,
    top_cell_pins_only: bool = False,
    declared_pins: frozenset[str] | None = None,
    apply_resistor_fixed_offset: bool = True,
    abstract_cell_patterns: tuple[str, ...] = (),
    abstract_instances: list[tuple[int, kdb.ICplxTrans]] | None = None,
    lef_macros: dict[str, tuple[str, dict[str, list[dict[str, Any]]]]] | None = None,
    abstract_cell_local_candidates: dict[int, dict[str, list[kdb.Point]]] | None = None,
    mom_net: str | None = None,
    mom_background_permittivity: float = MOM_CROSSCHECK_BACKGROUND_PERMITTIVITY,
    def_net_names: bool = False,
    critical_nets: frozenset[str] | None = None,
    def_pins: frozenset[str] | None = None,
) -> tuple[
    kdb.Netlist,
    list[str],
    tuple[list[dict[str, Any]], list[dict[str, Any]]] | None,
    list[dict[str, Any]],
    int,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any] | None,
]:
    """Build a flat ``LayoutToNetlist`` connectivity graph for ``deck`` and
    run device + netlist extraction.

    Flat (not hierarchical) extraction, deliberately: every layer is a
    single flattened ``Region``/``Texts`` collection over ``top_cell`` (via
    ``begin_shapes_rec``), the same whole-layout flattening idiom
    ``drc.py`` uses -- see ``docs/cli/extract.md``'s limitation note.

    ``apply_resistor_fixed_offset`` (issue #559): when ``True`` (the
    default -- unchanged behavior), :func:`_apply_device_parameter_corrections`
    adds each opted-in resistor device class's
    :attr:`~klayout_tools.decks.ResistorDevice.fixed_offset_ohm` to every
    extracted primitive here, before ``klt lvs``'s ``options.combine_devices``
    (if requested) folds series-connected primitives into one logical
    device -- KLayout's native ``Netlist.combine_devices()`` then sums each
    primitive's *already-corrected* ``R``, over-counting the fixed offset
    once per primitive instead of once per logical device. Callers that
    combine (``lvs.py``) pass ``False`` here and instead call
    :func:`apply_resistor_fixed_offset_corrections` themselves *after*
    combining, so the correction lands exactly once per surviving device
    object. The capacitor analogue
    (:attr:`~klayout_tools.decks.CapacitorDevice.perim_cap_f_um`) is
    unaffected by this flag -- it is proportional to each device's own
    perimeter, a quantity that itself sums linearly under
    ``combine_devices()``'s parallel-combine parameter summing, so applying
    it once per primitive is already equivalent to applying it once on the
    combined totals; see :func:`_apply_device_parameter_corrections` for the
    algebra.

    Returns ``(netlist, warnings, parasitic_nets, black_box_regions,
    dummy_devices_dropped, unmodelled_poly, abstracted_cells, dead_metal,
    mom_crosscheck)``.
    ``warnings`` is built from the extractor's own log entries (e.g. a gate
    touching no diffusion) -- non-fatal notes surfaced in the JSON response's
    ``warnings`` field. ``parasitic_nets`` is ``None`` unless
    ``parasitics_deck`` is given, in which case it is the
    ``(ground_nets, coupled_pairs)`` 2-tuple :func:`_compute_parasitics`
    returns, computed from ``LayoutToNetlist.polygons_of_net`` while ``l2n``
    is still alive (issue #760: ``coupled_pairs`` carries the vertical-overlap
    net-to-net coupling capacitance alongside the pre-existing per-net
    ground-RC list; issue #976: also lateral coupling, for any pair naming a
    ``critical_nets`` entry). ``mom_crosscheck`` (issue #798, ``klt extract
    --mom-net <net>``) is ``None`` unless ``mom_net`` is given, in which case
    it is :func:`_mom_ground_capacitance_for_net`'s result for the net named
    ``mom_net`` -- computed here, alongside ``parasitic_nets``, for the same
    reason: it needs ``l2n``/``circuit`` while they are still alive. When
    ``mom_net`` names a label shared by several distinct net islands, the
    lowest-``cluster_id`` one is solved and the ambiguity is appended to the
    result's ``warnings`` (issue #811); the result's ``net_id`` records which
    island that was, so ``run_extract`` can resolve the ground entry to swap
    by id instead of re-matching the name. A
    ``mom_net`` naming no net with ground-eligible parasitics geometry still
    returns a dict (with ``mom_capacitance_ff: None`` and an explanatory
    ``warnings`` entry), never ``None`` -- ``run_extract`` is what turns an
    unresolvable ``mom_net`` into a clean :class:`ExtractError`, matching
    every other "the caller asked for something that does not exist"
    validation in this module. ``black_box_regions`` is
    the JSON response's field (issue #293) -- see
    :func:`_resolve_black_box_regions` -- always a list, empty when the
    layout draws no reserved-annotation-layer geometry.
    ``dummy_devices_dropped`` is the number of devices suppressed by the
    deck's optional ``dummy`` marker layer -- MOS gates (issue #295),
    drawn resistors, and bipolars (both issue #462) alike -- ``0`` when no
    ``dummy`` layer is configured or no dummy geometry is drawn.
    ``unmodelled_poly`` is the JSON response's field (issue #324) -- see
    :func:`_detect_unmodelled_poly_bodies` -- always a list, one entry per
    poly component flagged by the unmodelled-device diagnostic, empty when
    ``warnings`` carries no unmodelled-device entry. ``dead_metal`` is the
    JSON response's field (issue #676) -- see :func:`_detect_dead_metal` --
    one entry per routing-stack cluster that joins no surviving net, empty
    on a layout whose every metal/via shape is netted.

    ``def_net_names`` (issue #951): when ``True``, each routed net is renamed
    to the DEF net name its geometry carries as GDS shape property
    :data:`_DEF_NET_NAME_PROPERTY_ID`, overriding the text-label-derived name
    ``extract_netlist()`` assigned. Probe points are collected up front (they
    must be read off raw shapes, before ``_resolve_black_box_regions`` can
    mask the metal regions) and applied straight after ``extract_netlist()``,
    before pin promotion and the purge passes read any net name -- see
    :func:`_def_net_name_probes` / :func:`_apply_def_net_name_overrides`.

    ``def_pins`` (issue #1390): the design's genuine top-level port net
    names, parsed off a routed DEF's own ``PINS`` section
    (``place_and_route.def_pin_names``) -- when given, applied as a
    reconciliation pass right after ``declared_pins``'s own, matching a
    promoted net's comma-joined label set (not just a whole-string match --
    see :func:`run_extract`'s own docstring for why) against this set and
    demoting every net with no intersection. ``None`` skips this entirely.

    ``abstract_cell_patterns``/``abstract_instances``/``lef_macros`` (issue
    #620): ``abstract_instances`` is the already-collected
    ``[(cell_index, transform), ...]`` list for every matched instance (see
    :func:`_collect_abstract_instances`) -- by the time this function runs,
    the *caller* (:func:`extract_netlist_from_layout`) has already erased
    each matched cell type's own device-recognition geometry from ``layout``
    in place, so nothing below this point needs to know about the
    abstraction to correctly extract the un-abstracted portion. This
    function's own responsibility is narrower: (1) exclude each abstracted
    instance's in-cell pin labels from the flat ``well_label``/
    ``poly_label``/``metal_labels`` collections (see
    :func:`_texts_excluding_abstract_cells`) -- otherwise an abstracted
    cell's own pin-name label would rename
    whatever top-level net happens to touch it -- and (2) once the flat
    netlist is extracted, wire every abstracted instance in as a black-box
    ``kdb.SubCircuit`` (:func:`_wire_abstract_cells`). ``abstracted_cells``
    is the JSON response's field -- always a list, empty when
    ``abstract_cell_patterns`` is empty (the default).
    ``abstract_cell_local_candidates`` (issue #1183) is
    :func:`_local_pin_candidate_points`'s per-matched-cell-type result,
    computed by the same caller from the *pre*-erasure geometry -- passed
    straight through to :func:`_wire_abstract_cells`; ``None`` (the default)
    disables the extra-candidate lookup entirely.
    """
    import klayout.db as kdb

    active = _region(layout, top_cell, deck.active)
    poly = _region(layout, top_cell, deck.poly)
    nwell = _region(layout, top_cell, deck.nwell)
    tap = _region(layout, top_cell, deck.tap)
    contact = _region(layout, top_cell, deck.contact)

    # A matched instance's own in-cell pin label is that pin's *name*, not a
    # top-level net name -- left in the flat label collection it would
    # rename (or comma-merge into) whatever net the parent's routing happens
    # to touch at that point (issue #620). No-op (same as `_texts`) when
    # `abstract_cell_patterns` is empty, the default.
    def _label_texts(layer: tuple[int, int] | None) -> kdb.Texts:
        if abstract_cell_patterns:
            return _texts_excluding_abstract_cells(
                layout, top_cell, layer, abstract_cell_patterns
            )
        return _texts(layout, top_cell, layer)

    well_label = _label_texts(deck.well_label)
    poly_label = _label_texts(deck.poly_label)
    metals = [_region(layout, top_cell, layer) for layer in deck.metals]
    metal_labels = [_label_texts(layer) for layer in deck.metal_labels]
    vias = [_region(layout, top_cell, layer) for layer in deck.vias]

    # `--def-net-names` (issue #951). Collected here, before
    # `_resolve_black_box_regions` below can mask `metals[]`: this reads
    # raw-shape properties straight off `layout`/`top_cell`, independently of
    # any Region built from them, and is only *applied* (via
    # `l2n.probe_net`) once extraction has run -- see the
    # `_apply_def_net_name_overrides` call site further down.
    def_net_name_probes = (
        _def_net_name_probes(layout, top_cell, deck.metals) if def_net_names else {}
    )

    # Black-box/abstract regions (#293), resolved *before* everything else
    # below (including the resistor resolution that follows): geometry
    # inside a marker shape on a reserved annotation layer (990-999, any
    # datatype -- issue #289) is masked out of every conductor/label region
    # first, so a resistor marker (or any other device-recognition geometry)
    # that happens to sit inside a black-box region is excluded outright
    # rather than "found" by a later device extractor and only then
    # short-circuited.
    (
        black_box_regions,
        active,
        poly,
        nwell,
        tap,
        contact,
        metals,
        vias,
        well_label,
        poly_label,
        metal_labels,
    ) = _resolve_black_box_regions(
        layout,
        top_cell,
        active,
        poly,
        nwell,
        tap,
        contact,
        metals,
        vias,
        well_label,
        poly_label,
        metal_labels,
    )

    # Dummy-device marker layer (issue #295, extended to resistors/bipolars
    # in #462): resolved *before* `_resolve_resistors` below so a resistor
    # recognition pass can subtract it from a candidate body the same way
    # the MOS gate-suppression block (further down) already does for
    # `nfet_gate`/`pfet_gate` -- see `_resolve_resistors`'s docstring for the
    # exact contract. `dummy_devices_dropped` accumulates across all three
    # recognition passes (resistor here, MOS and bipolar further below) into
    # a single JSON-response counter.
    dummy = _region(layout, top_cell, deck.dummy)
    dummy_devices_dropped = 0

    # Drawn precision resistors (#222), resolved *before* the MOS split
    # below: a recognised resistor body is cut out of its own conductor
    # layer, so (a) the two heads are no longer shorted through it, and (b)
    # a poly resistor crossing diffusion cannot also be mistaken for a gate
    # -- the same ordering both PDKs' own KLayout LVS decks use (sky130's
    # `tgate = poly.and(diff).not(poly_res)...`).
    (
        resistors,
        poly,
        active,
        metals,
        poly_resistor_candidate_bodies,
        resistor_dummy_dropped,
    ) = _resolve_resistors(layout, top_cell, deck, poly, active, metals, dummy)
    dummy_devices_dropped += resistor_dummy_dropped

    # MiM top-plate-via / bottom-plate overlap exclusion (issue #364): must
    # run before `vias` is registered into the netlist graph below and
    # before the generic per-layer `metals[i]`/`vias[i]` connectivity loop
    # consumes it -- see `_exclude_capacitor_top_via_overlap`'s docstring.
    # Without this, a capacitor's own `top_plate_via` (#314), placed per the
    # PDK's DRM-legal minimum-overlap requirement against its bottom plate,
    # is read by that generic loop as an ordinary via shorting the two
    # plates together.
    vias = _exclude_capacitor_top_via_overlap(layout, top_cell, deck, vias)

    # Derived tap for a PDK family with no distinct tap layer (issue #1084):
    # when the deck declares no `tap` but does declare one or both of
    # `tap_nplus`/`tap_pplus`, derive an equivalent tap region from the
    # deck's own `active`/`nwell` plus those implant layers -- exactly how
    # the PDK's own official LVS deck recognises a well/substrate tie with
    # no dedicated tap mask (see `ExtractionDeck.tap_nplus`/`tap_pplus`'s
    # docstring for the full derivation and doping-side reasoning). A well
    # tie is `tap_nplus`-covered diffusion *inside* `nwell` (opposite
    # doping from an ordinary PMOS source/drain, which is `tap_pplus`-
    # covered there); a substrate tie is `tap_pplus`-covered diffusion
    # *outside* every `nwell` (opposite doping from an ordinary NMOS
    # source/drain, `tap_nplus`-covered there) -- so this can never collide
    # with a real device's own source/drain diffusion. `- poly` drops any
    # sliver still crossed by a gate, matching the PDK's own "AND COMP NOT
    # Poly2" tie derivation the issue's own guidance cites.
    #
    # `tap` computed this way feeds the *same* tap/`tap_substrate`
    # connectivity mechanism below (issue #490) a directly-drawn `tap`
    # layer already uses, so nothing downstream needs a second, parallel
    # code path. `tap_declared` records whether *some* tap mechanism (drawn
    # or derived) exists for this deck -- gating the connectivity block
    # below the same way `deck.tap is not None` alone used to.
    tap_declared = deck.tap is not None
    if deck.tap is None and (deck.tap_nplus is not None or deck.tap_pplus is not None):
        tap_nplus_region = _region(layout, top_cell, deck.tap_nplus)
        tap_pplus_region = _region(layout, top_cell, deck.tap_pplus)
        tap = (
            (tap_nplus_region & active & nwell) | (tap_pplus_region & (active - nwell))
        ) - poly
        # Exclude the derived tie geometry from `active` before the NMOS/
        # PMOS source/drain split just below, so a tie strip is never also
        # registered as ordinary device-terminal diffusion (`nfet_sd`/
        # `pfet_sd`) -- mirroring how a dummy/resistor-body shape is
        # already cut out of `active`/`poly` above, before device
        # recognition runs.
        active = active - tap
        tap_declared = True

    # Per-flavour MOS marker split (issue #1111, option 2 of #552): a deck
    # may declare one or more `mos_flavours` entries (e.g. gf180mcu's
    # `Dualgate` 55/0, selecting its 5V/6V thick-oxide domain) narrowing this
    # deck's ordinary `active`/`nwell` MOS split to just the geometry drawn
    # inside a marker layer. Classified on the *undivided* `active` region's
    # own connected components -- before the nwell/poly split below -- since
    # a MOS device's active mask is drawn as one continuous polygon spanning
    # source-gate-drain, the natural per-device unit for this decision (see
    # `MOSFlavour`'s own docstring in `decks/__init__.py` for the full
    # derivation, including the marker-straddling policy: any overlap at all
    # claims the whole island for that flavour). Each flavour's claimed
    # geometry is removed from `active` before the default nfet/pfet split
    # further below, so the default split is unaffected outside every
    # flavour marker -- no regression for the common (unflavoured) case.
    flavour_active: list[kdb.Region] = []
    for flavour in deck.mos_flavours:
        marker_region = _region(layout, top_cell, flavour.marker)
        claimed = kdb.Region()
        if not marker_region.is_empty() and not active.is_empty():
            remaining = kdb.Region()
            for component in active.merged().each():
                component_region = kdb.Region(component)
                if not component_region.interacting(marker_region).is_empty():
                    claimed += component_region
                else:
                    remaining += component_region
            active = remaining
        flavour_active.append(claimed)

    # NMOS is active outside the well; PMOS is active inside it -- KLayout's
    # standard "well marks the flip side" MOS-splitting idiom (see
    # `ExtractionDeck`'s docstring). Splitting SD from the gate polygon
    # (rather than passing the undivided active region) is required by
    # `DeviceExtractorMOS4Transistor`'s "SD" input contract: it expects two
    # disjoint source/drain polygons per gate, which only exist once the
    # gate area is subtracted out.
    nfet_active = active - nwell
    pfet_active = active & nwell
    nfet_gate = nfet_active & poly
    pfet_gate = pfet_active & poly
    nfet_sd = nfet_active - poly
    pfet_sd = pfet_active - poly

    # Per-isolated-region substrate scoping (issue #1128): when the deck
    # declares `substrate_isolation` (e.g. gf180mcu's DNWELL), NMOS bodies
    # -- and any substrate-tie tap geometry (below) -- inside a connected
    # component of that layer get their own synthesized identity instead of
    # sharing the deck-wide `substrate_net` global. See
    # `ExtractionDeck.substrate_isolation`'s own docstring for the full
    # derivation. `isolation_islands` is sorted by bounding box (not raw
    # `Region` iteration order, which is not a documented stability
    # guarantee) so the per-island identities synthesized below are stable
    # across re-runs of the same layout. Empty -- and every downstream
    # partition below a guaranteed no-op via
    # `_partition_region_by_islands` -- for the overwhelmingly common case:
    # a deck that leaves this field `None` (every deck as of this field's
    # introduction until gf180mcu's own module sets it), or a layout that
    # draws no shapes on a deck's declared isolation layer at all.
    isolation_region = _region(layout, top_cell, deck.substrate_isolation)
    isolation_islands: list[kdb.Region] = sorted(
        (kdb.Region(component) for component in isolation_region.merged().each()),
        key=lambda region: (
            region.bbox().left,
            region.bbox().bottom,
            region.bbox().right,
            region.bbox().top,
        ),
    )
    # `nfet_active_outside`/`nfet_active_isolated` feed only the device-
    # recognition ("W" terminal) split below -- `nfet_gate`/`nfet_sd` above
    # stay the full (unpartitioned) region for every *other* purpose
    # (ordinary gate/SD net connectivity, the `_compute_parasitics` poly-
    # role gate exclusion), so a transistor's signal terminals route
    # normally regardless of which body-identity group it falls into.
    nfet_active_outside, nfet_active_isolated = _partition_region_by_islands(
        nfet_active, isolation_islands
    )
    nfet_gate_outside = nfet_active_outside & poly
    nfet_sd_outside = nfet_active_outside - poly
    nfet_gate_isolated = [region & poly for region in nfet_active_isolated]
    nfet_sd_isolated = [region - poly for region in nfet_active_isolated]

    # Same NMOS/PMOS + gate/SD split, per declared flavour (index-aligned
    # with `deck.mos_flavours`/`flavour_active` above).
    flavour_nfet_gate: list[kdb.Region] = []
    flavour_pfet_gate: list[kdb.Region] = []
    flavour_nfet_sd: list[kdb.Region] = []
    flavour_pfet_sd: list[kdb.Region] = []
    for claimed in flavour_active:
        f_nfet_active = claimed - nwell
        f_pfet_active = claimed & nwell
        flavour_nfet_gate.append(f_nfet_active & poly)
        flavour_pfet_gate.append(f_pfet_active & poly)
        flavour_nfet_sd.append(f_nfet_active - poly)
        flavour_pfet_sd.append(f_pfet_active - poly)

    # Unmodelled-device diagnostic (issue #288, split by marker presence in
    # #299, resistor-body/routing false positives narrowed in #324):
    # computed against `poly` as it stands right here -- *after*
    # `_resolve_resistors` has already cut out every resistor body this deck
    # *does* recognise, and *before* the blanket `l2n.connect(poly, contact)`
    # (and friends) below absorbs whatever is left into ordinary
    # interconnect. `poly_resistor_markers` is the raw union of every
    # declared `ResistorDevice.marker` on `poly` (unnarrowed by that entry's
    # own `requires`/`excludes`), letting the heuristic tell a "carries a
    # marker this deck knows about, but requires/excludes ruled it out" gap
    # apart from "carries no marker at all" -- see the function's docstring.
    # `poly_resistor_bodies` is the union of every *candidate* resistor body
    # `_resolve_resistors` returned above (already narrowed by
    # requires/excludes, but -- unlike `resistors` itself -- **before** any
    # `dummy` cut, issue #462) -- a poly component abutting one of these is
    # that resistor's own terminal head, by construction, so it must never
    # be flagged as a candidate unmodelled-device body (issue #324), even
    # when the resistor itself was fully dummy-suppressed and so carries no
    # surviving entry in `resistors`.
    poly_resistor_markers = kdb.Region()
    for spec in deck.resistors:
        if spec.body == deck.poly:
            poly_resistor_markers += _region(layout, top_cell, spec.marker)
    poly_resistor_bodies = poly_resistor_candidate_bodies
    # Combine the default nfet/pfet gates with every declared flavour's own
    # (issue #1111): a flavoured transistor's gate is a real, recognised MOS
    # gate just like the default split's, and must not be misflagged as
    # unmodelled poly merely because it is not part of `nfet_gate`/
    # `pfet_gate` specifically.
    all_nfet_gate = nfet_gate
    for gate in flavour_nfet_gate:
        all_nfet_gate = all_nfet_gate + gate
    all_pfet_gate = pfet_gate
    for gate in flavour_pfet_gate:
        all_pfet_gate = all_pfet_gate + gate
    unmodelled_device_warnings, unmodelled_poly = _detect_unmodelled_poly_bodies(
        poly,
        contact,
        all_nfet_gate,
        all_pfet_gate,
        poly_resistor_markers,
        poly_resistor_bodies,
        layout.dbu,
    )

    # Dummy-device suppression, MOS gates (issue #295; extended to resistor
    # and bipolar recognition in #462 -- see the resistor pass above and the
    # bipolar pass below): a deck may declare an optional `dummy` marker
    # layer (see `ExtractionDeck.dummy`) covering drawn-but-non-functional
    # dummy devices -- the matched-pair/array edge fill whose gate and
    # diffusions are tied off to a rail. A MOS gate lying under that marker
    # must not become a device in the extracted netlist (otherwise every
    # dummy is a spurious `device.unmatched` under `klt lvs`), so subtract
    # the marker (already resolved above, before `_resolve_resistors`) from
    # the NMOS/PMOS gate regions *before* device recognition: a gate fully
    # covered by the marker is never handed to `extract_devices` and so is
    # never recognised as a device at all. Only the gate is cut -- the
    # dummy's diffusions (`nfet_sd`/`pfet_sd`) and its gate poly stay in
    # `poly`, so they still participate in ordinary connectivity below and
    # tie off to the rail exactly as drawn.
    #
    # Ordered *after* the unmodelled-device diagnostic above (so a dummy gate
    # is still recognised as a gate there and never misflagged as unmodelled
    # poly) and *before* registration/extraction below (so the suppressed gate
    # area reaches neither the device extractor nor the parasitics pass).
    # `dummy_devices_dropped` counts gate components fully consumed by the
    # marker -- a device that genuinely vanishes -- using the same
    # `region.merged().each()` connected-component idiom as
    # `_detect_unmodelled_poly_bodies`. A marker only partially covering a
    # gate is a clean geometric cut, not a dropped device: the remaining gate
    # area still extracts, so it is not counted.
    if not dummy.is_empty():
        for gate in (nfet_gate, pfet_gate, *flavour_nfet_gate, *flavour_pfet_gate):
            for component in gate.merged().each():
                if (kdb.Region(component) - dummy).is_empty():
                    dummy_devices_dropped += 1
        nfet_gate = nfet_gate - dummy
        pfet_gate = pfet_gate - dummy
        flavour_nfet_gate = [gate - dummy for gate in flavour_nfet_gate]
        flavour_pfet_gate = [gate - dummy for gate in flavour_pfet_gate]

    l2n = kdb.LayoutToNetlist(top_cell.name, layout.dbu)
    # `register` returns the layer index `polygons_of_net(net, index)` needs
    # for the per-net geometry the parasitics pass reads back (see
    # `_compute_parasitics`); capture the ones the RC roles map to.
    layer_index: dict[str, int] = {}
    for name, region in [
        ("nfet_sd", nfet_sd),
        ("nfet_gate", nfet_gate),
        ("pfet_sd", pfet_sd),
        ("pfet_gate", pfet_gate),
        ("poly", poly),
        ("contact", contact),
        ("nwell", nwell),
        ("tap", tap),
    ]:
        layer_index[name] = l2n.register(region, name)
    # Per-flavour MOS SD/gate regions (issue #1111), registered under their
    # own `nfet_sd_<flavour>`/`pfet_gate_<flavour>`-style names into the same
    # `layer_index` map -- kept distinct from the default `nfet_sd`/
    # `nfet_gate` entries above (rather than folded into a union under the
    # same name) so each flavour's own `extract_devices` call below feeds it
    # *only* that flavour's geometry, never double-counting the default
    # split. `_compute_parasitics` folds these back into its diffusion/poly
    # roles alongside the default pair (see its own docstring).
    for flavour, f_nfet_sd, f_nfet_gate, f_pfet_sd, f_pfet_gate in zip(
        deck.mos_flavours,
        flavour_nfet_sd,
        flavour_nfet_gate,
        flavour_pfet_sd,
        flavour_pfet_gate,
        strict=True,
    ):
        for name, region in [
            (f"nfet_sd_{flavour.flavour}", f_nfet_sd),
            (f"nfet_gate_{flavour.flavour}", f_nfet_gate),
            (f"pfet_sd_{flavour.flavour}", f_pfet_sd),
            (f"pfet_gate_{flavour.flavour}", f_pfet_gate),
        ]:
            layer_index[name] = l2n.register(region, name)
    metal_index: list[int] = []
    for index, region in enumerate(metals):
        metal_index.append(l2n.register(region, f"metal{index}"))
    via_index: list[int] = []
    for index, region in enumerate(vias):
        via_index.append(l2n.register(region, f"via{index}"))
    l2n.register(well_label, "well_label")
    l2n.register(poly_label, "poly_label")
    for index, texts in enumerate(metal_labels):
        l2n.register(texts, f"metal{index}_label")

    # NMOS body (issue #490). `nfet_body` itself stays a permanently empty
    # placeholder `Region` -- deliberately never touched by an ordinary
    # `l2n.connect()` call anywhere below. It is shared, as-is, by every
    # recognised NMOS device's "W" terminal below, by any declared
    # `bulk_to_substrate` resistor's "W" terminal (#222), and by a
    # collector-less bipolar's collector terminal (`bipolar_collector`
    # further down) -- and KLayout's `LayoutToNetlist` does not tolerate an
    # ordinary inter-layer `connect()` declaration against a region that is
    # simultaneously used as a *shared* device-terminal input across more
    # than one recognised device: it was empirically found to corrupt
    # *unrelated* terminals' connectivity (observed: two independent NMOS
    # devices' gate nets, and their otherwise-distinct real tap-ring nets,
    # all collapsing onto one shared node) rather than raising or silently
    # no-op'ing. `connect_global` alone -- never plain `connect()` -- is the
    # only supported way to give this placeholder a net identity.
    #
    # `tap` (already resolved above -- drawn, or derived per issue #1084's
    # `tap_declared` block) serves double duty in this curated deck: a shape
    # drawn/derived *inside* `nwell` is a PMOS well tie (handled below,
    # unchanged), a shape drawn/derived *outside* `nwell` sits on native
    # P-substrate and is a genuine, drawable substrate tie. `tap_substrate`
    # is that outside-the-well slice -- real, possibly-empty geometry (empty
    # exactly when the deck has no tap mechanism at all, drawn or derived
    # (`tap_declared` is `False`), or when no tap shape happens to sit
    # outside every `nwell` in this particular layout) -- registered as its
    # own, ordinary (not a device terminal) layer, so it is safe to
    # `connect()` to `contact`/the metal stack the same way the well-tie
    # slice already is (see the `tap_declared` connectivity block below). It
    # is then tied to `nfet_body`'s shared identity purely via
    # `connect_global` using the *same* global name (`deck.substrate_net`)
    # -- `connect_global` unifies every layer/region tied to a given name
    # into one net regardless of geometric overlap between them, so a drawn
    # (or derived) tap ring's real, routed net and every device sharing the
    # (still-empty) `nfet_body` placeholder land on that one net together,
    # while a layout with no drawn tap ring at all (`tap_substrate` empty)
    # still falls back to exactly the same synthesized `substrate_net`
    # identity as before this fix.
    nfet_body = kdb.Region()
    l2n.register(nfet_body, "nfet_body")
    tap_substrate = tap - nwell
    l2n.register(tap_substrate, "tap_substrate")

    # Per-isolated-region body placeholders (issue #1128): one additional,
    # equally-empty placeholder `Region` per `isolation_islands` entry,
    # registered under its own name -- the exact same "shared, permanently
    # empty, `connect_global`-only" contract `nfet_body` above documents,
    # just one instance per isolated region instead of one for the whole
    # layout. `tap_substrate` is likewise split into the slice outside
    # every isolation island (`tap_substrate_outside` -- ties to
    # `nfet_body`/`deck.substrate_net` exactly as `tap_substrate` did before
    # this field existed) and one slice per island
    # (`tap_substrate_isolated`, aligned with `nfet_body_isolated`) tied to
    # that island's own synthesized identity instead. Both partitions are
    # true no-ops (`tap_substrate_outside is tap_substrate`,
    # `nfet_body_isolated == []`) when `isolation_islands` is empty.
    nfet_body_isolated: list[kdb.Region] = []
    for index in range(len(isolation_islands)):
        island_body = kdb.Region()
        l2n.register(island_body, f"nfet_body_iso{index}")
        nfet_body_isolated.append(island_body)
    tap_substrate_outside, tap_substrate_isolated = _partition_region_by_islands(
        tap_substrate, isolation_islands
    )
    if isolation_islands:
        l2n.register(tap_substrate_outside, "tap_substrate_outside")
        for index, isolated_slice in enumerate(tap_substrate_isolated):
            l2n.register(isolated_slice, f"tap_substrate_iso{index}")

    nfet_extractor = kdb.DeviceExtractorMOS4Transistor(deck.nfet_class)
    pfet_extractor = kdb.DeviceExtractorMOS4Transistor(deck.pfet_class)
    if isolation_islands:
        # Devices whose active island overlaps no isolation component still
        # extract through this same default pass, just against the
        # `_outside` subset of `nfet_sd`/`nfet_gate` rather than the full
        # region -- the per-island pass just below extracts the rest, so
        # together they cover exactly the same geometry `nfet_sd`/
        # `nfet_gate` do, split only by which "W" placeholder each
        # transistor's recognised device lands on.
        l2n.register(nfet_sd_outside, "nfet_sd_substrate_outside")
        l2n.register(nfet_gate_outside, "nfet_gate_substrate_outside")
        l2n.extract_devices(
            nfet_extractor,
            {"SD": nfet_sd_outside, "G": nfet_gate_outside, "W": nfet_body},
        )
    else:
        l2n.extract_devices(
            nfet_extractor, {"SD": nfet_sd, "G": nfet_gate, "W": nfet_body}
        )
    l2n.extract_devices(pfet_extractor, {"SD": pfet_sd, "G": pfet_gate, "W": nwell})

    # One additional `nfet` extraction pass per isolated region (issue
    # #1128), mirroring the `mos_flavours` "additional pass, same device
    # class" pattern just below: reuses `deck.nfet_class` (not a distinct
    # class), so KLayout folds these devices into the same
    # `DeviceClassMOS4Transistor` the default pass above created --
    # `devices[].class`/`device_counts` are unaffected by isolation
    # scoping, only each device's own "W"/body net identity is. Applies
    # only to this deck's *default* (non-`mos_flavours`) NMOS recognition
    # -- see `ExtractionDeck.substrate_isolation`'s docstring for the
    # documented flavour/isolation interaction gap.
    for index, (sd_region, gate_region, body_region) in enumerate(
        zip(nfet_sd_isolated, nfet_gate_isolated, nfet_body_isolated, strict=True)
    ):
        l2n.register(sd_region, f"nfet_sd_iso{index}")
        l2n.register(gate_region, f"nfet_gate_iso{index}")
        island_extractor = kdb.DeviceExtractorMOS4Transistor(deck.nfet_class)
        l2n.extract_devices(
            island_extractor, {"SD": sd_region, "G": gate_region, "W": body_region}
        )

    # Per-flavour MOS device extraction (issue #1111): one *additional*
    # `nfet`/`pfet` extraction pass per declared flavour, against that
    # flavour's own SD/gate regions (registered above). Reuses the deck's
    # ordinary `nfet_class`/`pfet_class` for the extractor's class name --
    # not a distinct class -- so KLayout folds every pass's devices into the
    # same two `DeviceClassMOS4Transistor` objects the default pair above
    # created (empirically confirmed: `LayoutToNetlist` looks up/reuses a
    # device class by name rather than creating a duplicate), leaving
    # `devices[].class` and `device_counts` unaffected by flavour (see
    # `MOSFlavour`'s own docstring for why). Each newly-added device is then
    # tagged with `MOS_FLAVOUR_PROPERTY` (a KLayout device *property*, not a
    # netlist-visible terminal/parameter) so only the `--pdk` model-binding
    # writer (`pdk_models.create_model_binding_delegate`) can tell it apart
    # from a default-flavour device, to select the flavour's own real
    # subcircuit. `l2n.netlist()` is safe to call more than once mid-
    # construction (empirically confirmed: it returns a live view of the
    # netlist built so far, and further `extract_devices`/`connect()` calls
    # after it keep working normally) -- taking a before/after device-id
    # snapshot around each flavour's own `extract_devices` call is how the
    # newly-added devices are identified without needing any geometric
    # correlation back to a device after the fact.
    #
    # A flavoured NMOS device's own "W" terminal is always `nfet_body` (the
    # deck-wide outside/global identity) below, never one of
    # `nfet_body_isolated` -- issue #1128's per-isolated-region scoping is
    # not applied to `mos_flavours` geometry. A flavoured transistor whose
    # active island happens to sit inside an isolation region still resolves
    # to `deck.substrate_net`, exactly as every NMOS device did before
    # #1128 existed: a known, documented residual gap (see
    # `ExtractionDeck.substrate_isolation`'s docstring), not a regression.
    for flavour, f_nfet_sd, f_nfet_gate, f_pfet_sd, f_pfet_gate in zip(
        deck.mos_flavours,
        flavour_nfet_sd,
        flavour_nfet_gate,
        flavour_pfet_sd,
        flavour_pfet_gate,
        strict=True,
    ):
        circuit = l2n.netlist().circuit_by_name(top_cell.name)
        before_ids = {device.id() for device in circuit.each_device()}
        f_nfet_extractor = kdb.DeviceExtractorMOS4Transistor(deck.nfet_class)
        l2n.extract_devices(
            f_nfet_extractor, {"SD": f_nfet_sd, "G": f_nfet_gate, "W": nfet_body}
        )
        f_pfet_extractor = kdb.DeviceExtractorMOS4Transistor(deck.pfet_class)
        l2n.extract_devices(
            f_pfet_extractor, {"SD": f_pfet_sd, "G": f_pfet_gate, "W": nwell}
        )
        for device in l2n.netlist().circuit_by_name(top_cell.name).each_device():
            if device.id() not in before_ids:
                device.set_property(MOS_FLAVOUR_PROPERTY, flavour.flavour)

    # Bipolar (BJT) device recognition (issue #223): each of the deck's
    # optional `bipolars` entries (see `BipolarDevice` in `decks/__init__.py`)
    # scopes recognition to genuine device-cell instances by intersecting the
    # deck's own MOS-recognition `base`/`emitter` layers with the PDK's
    # bipolar device-mark `marker` layer *before* handing them to KLayout's
    # `DeviceExtractorBJT3Transistor` -- without that intersection, every
    # ordinary PMOS nwell in the layout would be misrecognised as a bipolar
    # base. `bipolar_regions` carries the built regions through to the
    # connectivity section below (registration/extraction must happen once
    # per entry, before any layer can be used in a `connect()` call).
    #
    # Dummy-device suppression (issue #295, extended to bipolars in #462):
    # `dummy` (resolved above, before `_resolve_resistors`) is subtracted
    # from `bipolar_base` *before* the emitter/collector regions are derived
    # from it -- mirroring the MOS gate-suppression block's "cut before
    # recognition" idiom, and (because both `bipolar_emitter` and
    # `bipolar_collector` are themselves intersections against `bipolar_base`
    # below) the cut propagates to all three terminals for free, so a
    # dummy-covered bipolar unit is dropped as a single connected whole
    # rather than leaving an orphaned emitter/collector fragment behind. A
    # base component fully consumed by the marker is counted into the
    # shared `dummy_devices_dropped` counter; a component only partially
    # covered survives as a clean geometric cut, matching the MOS behaviour.
    bipolar_regions: list[tuple[BipolarDevice, kdb.Region, kdb.Region, kdb.Region]] = []
    for bipolar in deck.bipolars:
        bipolar_base_layer = _region(layout, top_cell, bipolar.base)
        bipolar_marker = _region(layout, top_cell, bipolar.marker)
        bipolar_emitter_layer = _region(layout, top_cell, bipolar.emitter)
        bipolar_base = bipolar_base_layer & bipolar_marker
        if not dummy.is_empty():
            for component in bipolar_base.merged().each():
                if (kdb.Region(component) - dummy).is_empty():
                    dummy_devices_dropped += 1
            bipolar_base = bipolar_base - dummy
        bipolar_emitter = bipolar_emitter_layer & bipolar_base
        # Narrow the emitter the same requires/excludes idiom the resistor
        # (`_resolve_resistors`) and capacitor blocks use, so a base-contact
        # ring drawn on the *same* diffusion layer inside the same marked base
        # is not misrecognised as a second emitter/device (issue #302). No-op
        # when the entry declares neither field (both default to `()`).
        for layer in bipolar.emitter_requires:
            bipolar_emitter = bipolar_emitter & _region(layout, top_cell, layer)
        for layer in bipolar.emitter_excludes:
            bipolar_emitter = bipolar_emitter - _region(layout, top_cell, layer)
        l2n.register(bipolar_base, f"{bipolar.class_name}_base")
        l2n.register(bipolar_emitter, f"{bipolar.class_name}_emitter")
        if bipolar.collector is not None:
            bipolar_collector_layer = _region(layout, top_cell, bipolar.collector)
            bipolar_collector = bipolar_collector_layer & bipolar_base
        else:
            # No drawn collector layer for this PDK's vertical bipolar --
            # `DeviceExtractorBJT3Transistor` treats an empty `C` input as
            # "collector formed by the substrate" and outputs the base
            # region's own footprint onto it (see `BipolarDevice`'s
            # docstring); `connect_global` below ties that footprint to the
            # deck's substrate net, mirroring `nfet_body` above.
            bipolar_collector = kdb.Region()
        l2n.register(bipolar_collector, f"{bipolar.class_name}_collector")

        bjt_extractor = kdb.DeviceExtractorBJT3Transistor(bipolar.class_name)
        l2n.extract_devices(
            bjt_extractor,
            {"B": bipolar_base, "E": bipolar_emitter, "C": bipolar_collector},
        )
        bipolar_regions.append(
            (bipolar, bipolar_base, bipolar_emitter, bipolar_collector)
        )

    # Junction-diode device recognition (issue #542): each of the deck's
    # optional `diodes` entries (see `DiodeDevice` in `decks/__init__.py`)
    # builds its two terminal regions -- both scoped to the PDK's diode
    # device-mark layer and narrowed by per-terminal implant
    # `requires`/`excludes` -- and hands them to KLayout's native
    # `DeviceExtractorDiode`, which forms the device from their geometric
    # overlap. Without this, a discrete PN/ESD-clamp diode extracts as no
    # device at all (nothing recognises the diffusion-in-well junction), so
    # `klt lvs` cannot verify any diode-based clamp.
    #
    # `diode_regions` carries the built regions through to the connectivity
    # section below (registration/extraction must happen once per entry,
    # before any layer can be used in a `connect()` call), mirroring
    # `bipolar_regions` above.
    diode_regions: list[tuple[DiodeDevice, kdb.Region, kdb.Region]] = []
    for diode in deck.diodes:
        # Deck-authoring validation, checked unconditionally (like the
        # capacitor block's own `top_plate_via` pairing check) so a mistake
        # in a deck module surfaces even on a diode-free layout: a diode
        # with *no* drawn terminal at all has nothing to overlap and would
        # silently extract nothing.
        if diode.anode is None and diode.cathode is None:
            raise ExtractError(
                f"diode '{diode.name}': at most one of anode/cathode may be "
                "None (the substrate-formed terminal) -- a diode with neither "
                "terminal drawn cannot be recognised"
            )

        diode_marker = _region(layout, top_cell, diode.marker)
        anode_region = _diode_terminal_region(
            layout,
            top_cell,
            diode_marker,
            diode.anode,
            diode.anode_requires,
            diode.anode_excludes,
        )
        cathode_region = _diode_terminal_region(
            layout,
            top_cell,
            diode_marker,
            diode.cathode,
            diode.cathode_requires,
            diode.cathode_excludes,
        )

        # Dummy-device suppression (issue #295, extended to resistors and
        # bipolars in #462, to diodes here): count and cut whole recognised
        # junctions covered by the deck's `dummy` marker. Counting is done
        # against the *recognised junction* (the anode/cathode overlap --
        # exactly what `DeviceExtractorDiode` turns into a device) rather
        # than against the raw marker layer, because a deck may declare
        # several diode flavours sharing one device-mark layer (gf180mcu
        # declares two on `diode_mk`); counting marker components would
        # then charge the same dummy device once per declared flavour.
        # A junction only partially covered survives as a clean geometric
        # cut, matching the MOS/bipolar behaviour.
        if not dummy.is_empty():
            for component in (anode_region & cathode_region).merged().each():
                if (kdb.Region(component) - dummy).is_empty():
                    dummy_devices_dropped += 1
            anode_region = anode_region - dummy
            cathode_region = cathode_region - dummy

        if anode_region.is_empty() or cathode_region.is_empty():
            # No diode marker (or no matching implant geometry) drawn
            # anywhere on this layout -- the common case. Registering and
            # extracting empty regions would be a no-op anyway, but skipping
            # keeps a diode-free layout's extraction bit-for-bit what it was
            # before this feature existed, the same guard the capacitor
            # block below applies.
            continue

        l2n.register(anode_region, f"{diode.name}_anode")
        l2n.register(cathode_region, f"{diode.name}_cathode")
        l2n.extract_devices(
            kdb.DeviceExtractorDiode(diode.name),
            {"P": anode_region, "N": cathode_region},
        )
        diode_regions.append((diode, anode_region, cathode_region))

    # MiM capacitor device recognition (issue #225): each of the deck's
    # optional `capacitors` entries (see `CapacitorDevice` in
    # `decks/__init__.py`) derives its own top-plate/bottom-plate geometry --
    # narrowed by device-specific `requires`/`excludes` layers, and (for a
    # PDK whose bottom plate is an ordinary routing metal rather than a
    # purpose-drawn cap layer) the PDK's own "virtual bottom plate" oversize
    # derivation -- then hands the two plate regions straight to KLayout's
    # native `DeviceExtractorCapacitor`. Unlike the bipolar block above,
    # neither plate layer is one of this deck's own MOS-recognition layers,
    # so there is nothing to intersect against other than the device's own
    # declared layers.
    for capacitor in deck.capacitors:
        # Deck-authoring validation (issue #314): checked unconditionally,
        # like `_resolve_resistors`'s own `_conductor` helper, so a mistake
        # in a deck module is caught even on a cap-free layout rather than
        # only surfacing once someone draws a MiM cap.
        if (capacitor.top_plate_via is None) != (capacitor.top_plate_via_metal is None):
            raise ExtractError(
                f"capacitor '{capacitor.name}': top_plate_via and "
                "top_plate_via_metal must both be set or both be left unset"
            )
        if (
            capacitor.top_plate_via_metal is not None
            and capacitor.top_plate_via_metal not in deck.metals
        ):
            layer, datatype = capacitor.top_plate_via_metal
            raise ExtractError(
                f"capacitor '{capacitor.name}': top_plate_via_metal "
                f"{layer}/{datatype} is not one of the deck's metals[] layers"
            )

        # Plate geometry derivation shared with the top-plate-via/bottom-
        # plate overlap exclusion above (#364) -- see
        # `_capacitor_plate_regions`'s docstring for why the two must never
        # drift apart on what counts as "this capacitor's bottom plate".
        top_region, bottom_region = _capacitor_plate_regions(
            layout, top_cell, capacitor
        )

        # Dummy-device suppression (issue #295, extended to resistors and
        # bipolars in #462, to diodes in #542, to capacitors here): count and
        # cut whole recognised top-plate components covered by the deck's
        # `dummy` marker. Counting (and cutting) is scoped to `top_region`
        # alone -- the device-defining plate, mirroring the diode block's
        # single-region precedent above -- rather than `bottom_region`, which
        # a matched cap array typically shares across multiple devices (a
        # dummy-covered bottom plate would still be a live node for its
        # non-dummy neighbours). A top-plate component only partially covered
        # survives as a clean geometric cut, matching the MOS/bipolar/diode
        # behaviour.
        if not dummy.is_empty():
            for component in top_region.merged().each():
                if (kdb.Region(component) - dummy).is_empty():
                    dummy_devices_dropped += 1
            top_region = top_region - dummy

        if top_region.is_empty() or bottom_region.is_empty():
            # No PDK cap marker drawn anywhere on this layout -- the common
            # case. Registering/extracting an empty device would be a no-op
            # anyway, but skipping it entirely keeps a cap-free layout's
            # extraction bit-for-bit what it was before this feature existed.
            continue

        # Each plate is its own new, self-connected node: `connect()` merges
        # polygons of the *same* plate that touch (e.g. a shared bottom
        # plate across several caps).
        l2n.register(bottom_region, f"{capacitor.name}_bottom")
        l2n.register(top_region, f"{capacitor.name}_top")
        l2n.connect(bottom_region)
        l2n.connect(top_region)

        # Bottom-plate connectivity (issue #314): when the declared
        # `bottom_plate` conductor is one of this deck's own tracked
        # `metals[]` layers, tie the recognised (possibly virtual-plate-
        # clipped) bottom region into that metal's connectivity node, so
        # ordinary contact/via/metal routing to that metal reaches this
        # terminal instead of leaving it an isolated, anonymous net -- see
        # `CapacitorDevice`'s docstring.
        if capacitor.bottom_plate in deck.metals:
            bottom_metal_index = deck.metals.index(capacitor.bottom_plate)
            l2n.connect(bottom_region, metals[bottom_metal_index])

        # Top-plate connectivity (issue #314): when the deck declares the
        # via layer that lands on the top plate and the metal it lands on
        # (`top_plate_via`/`top_plate_via_metal`), wire the top plate
        # through that via into the corresponding `metals[]` node -- the
        # top-plate analogue of the bottom-plate wiring above. Left unwired
        # (isolated node, documented) when the deck declares neither field.
        if capacitor.top_plate_via is not None:
            top_via_region = _region(layout, top_cell, capacitor.top_plate_via)
            l2n.register(top_via_region, f"{capacitor.name}_top_via")
            l2n.connect(top_via_region)
            l2n.connect(top_region, top_via_region)
            top_via_metal_index = deck.metals.index(capacitor.top_plate_via_metal)
            l2n.connect(top_via_region, metals[top_via_metal_index])

        l2n.extract_devices(
            kdb.DeviceExtractorCapacitor(capacitor.name, capacitor.area_cap_f_um2),
            {"P1": bottom_region, "P2": top_region},
        )

    # Drawn resistors: `R` is the recognised resistive segment, `C` the
    # terminal (contacted head) region -- the same conductor layer the
    # segment was cut out of, already part of the connectivity graph below,
    # so the heads pick up their nets from ordinary contact/metal routing.
    # The body itself is deliberately never `connect`ed to anything: that is
    # precisely what stops it being a short.
    for index, (spec, body, terminal) in enumerate(resistors):
        l2n.register(body, f"res{index}_body")
        if spec.bulk_to_substrate:
            l2n.extract_devices(
                kdb.DeviceExtractorResistorWithBulk(spec.name, spec.sheet_rho_ohm_sq),
                {"R": body, "C": terminal, "W": nfet_body},
            )
        else:
            l2n.extract_devices(
                kdb.DeviceExtractorResistor(spec.name, spec.sheet_rho_ohm_sq),
                {"R": body, "C": terminal},
            )

    warnings = [
        str(entry.message) for entry in l2n.each_log_entry()
    ] + unmodelled_device_warnings

    # Connectivity. Deliberately does *not* connect `nwell`/`tap` to
    # `contact` as a blanket rule -- see `ExtractionDeck`'s docstring: the
    # well is a background region spanning the whole PMOS area, so a
    # blanket well<->contact connect would short every terminal inside the
    # well together. Only a genuinely distinct `tap` region (drawn or
    # derived -- `tap_declared`, issue #1084) is safe to tie the well to
    # directly.
    l2n.connect(nfet_sd)
    l2n.connect(pfet_sd)
    l2n.connect(nfet_gate)
    l2n.connect(pfet_gate)
    l2n.connect(poly)
    l2n.connect(nfet_gate, poly)
    l2n.connect(pfet_gate, poly)
    # Same connectivity, per declared MOS flavour (issue #1111) -- mirrors
    # the default pair above exactly, just against each flavour's own SD/gate
    # regions.
    for f_nfet_sd, f_nfet_gate, f_pfet_sd, f_pfet_gate in zip(
        flavour_nfet_sd,
        flavour_nfet_gate,
        flavour_pfet_sd,
        flavour_pfet_gate,
        strict=True,
    ):
        l2n.connect(f_nfet_sd)
        l2n.connect(f_pfet_sd)
        l2n.connect(f_nfet_gate)
        l2n.connect(f_pfet_gate)
        l2n.connect(f_nfet_gate, poly)
        l2n.connect(f_pfet_gate, poly)
    # Same connectivity again, this time against the isolation-split "SD"/
    # "G" regions actually passed to `extract_devices` above (issue #1128):
    # KLayout ties a device terminal's connectivity to the *exact*
    # registered layer object passed as that terminal, not merely to its
    # underlying geometry, so `nfet_sd_outside`/`nfet_sd_isolated[i]` (and
    # the matching gate regions) need their own `connect()` calls here even
    # though they physically overlap `nfet_sd`/`nfet_gate` above -- both
    # ends geometrically meet at `contact`/`poly`, so this still resolves
    # into the exact same merged nets, just reached through two registered
    # layers instead of one. A no-op block (`isolation_islands` empty) for
    # every deck that leaves `substrate_isolation` unset.
    if isolation_islands:
        l2n.connect(nfet_sd_outside)
        l2n.connect(nfet_gate_outside)
        l2n.connect(nfet_gate_outside, poly)
        l2n.connect(nfet_sd_outside, contact)
        for sd_region, gate_region in zip(
            nfet_sd_isolated, nfet_gate_isolated, strict=True
        ):
            l2n.connect(sd_region)
            l2n.connect(gate_region)
            l2n.connect(gate_region, poly)
            l2n.connect(sd_region, contact)
    l2n.connect(nwell)
    if tap_declared:
        l2n.connect(tap)
        l2n.connect(nwell, tap)
        l2n.connect(tap, contact)
        # Substrate-tie slice of `tap` (issue #490): `tap_substrate` is a
        # *different*, ordinary (non-device-terminal) registered layer than
        # `tap` above, even though its geometry -- where present -- is a
        # literal subset of it, so it needs its own `contact` connection to
        # join the same metal-routed net a tap ring's shapes reach via
        # `tap`/`contact` above. Safe to `connect()` normally here (unlike
        # `nfet_body` above): `tap_substrate` is never passed to
        # `extract_devices` as a terminal. Split by isolated region (issue
        # #1128): `tap_substrate_outside` is `tap_substrate` itself when
        # `isolation_islands` is empty, so this is a no-op change in the
        # common case; each `tap_substrate_isolated` slice gets its own
        # `contact` connection the same way.
        l2n.connect(tap_substrate_outside, contact)
        for isolated_slice in tap_substrate_isolated:
            l2n.connect(isolated_slice, contact)
    l2n.connect(nwell, well_label)
    # Name a poly/gate node directly off a text on the poly-label layer -- the
    # only way a bare-poly gate (no contact/metal landing pad) can carry a
    # `klt gen-compose` `pins[]` label into extraction as a named pin (#210).
    # No-op when the deck declares no `poly_label` (empty Texts) or no text is
    # drawn on it.
    l2n.connect(poly, poly_label)
    l2n.connect(contact)
    l2n.connect(nfet_sd, contact)
    l2n.connect(pfet_sd, contact)
    for f_nfet_sd, f_pfet_sd in zip(flavour_nfet_sd, flavour_pfet_sd, strict=True):
        l2n.connect(f_nfet_sd, contact)
        l2n.connect(f_pfet_sd, contact)
    l2n.connect(poly, contact)

    if metals:
        l2n.connect(contact, metals[0])
        l2n.connect(metals[0])
        if metal_labels and metal_labels[0] is not None:
            l2n.connect(metals[0], metal_labels[0])
        for index in range(len(vias)):
            l2n.connect(metals[index], vias[index])
            l2n.connect(vias[index])
            l2n.connect(vias[index], metals[index + 1])
            l2n.connect(metals[index + 1])
            if index + 1 < len(metal_labels) and metal_labels[index + 1] is not None:
                l2n.connect(metals[index + 1], metal_labels[index + 1])

    l2n.connect_global(nfet_body, deck.substrate_net)
    # `connect_global` with the *same* name on a second, ordinary (non-
    # device-terminal) layer merges it into the identical net (see the
    # `nfet_body`/`tap_substrate` docstring above) -- the only supported way
    # to give a drawn substrate-tap ring's real net the same identity as
    # every device's `nfet_body` terminal.
    l2n.connect_global(tap_substrate_outside, deck.substrate_net)

    # Per-isolated-region substrate identities (issue #1128): each
    # `isolation_islands[i]` bucket gets its own placeholder body region
    # (`nfet_body_isolated[i]`, extracted above) and its own slice of any
    # substrate-tie tap geometry landing inside that island
    # (`tap_substrate_isolated[i]`) tied together via `connect_global` under
    # a per-island synthesized name -- the same "empty placeholder +
    # `connect_global`" mechanism the deck-wide `substrate_net` identity
    # above uses, just scoped to geometry physically inside one connected
    # component of `deck.substrate_isolation` instead of the whole layout.
    # A no-op loop (`nfet_body_isolated == []`) whenever `isolation_islands`
    # is empty.
    for index, island_body in enumerate(nfet_body_isolated):
        island_net = f"{deck.substrate_net}_iso{index}"
        l2n.connect_global(island_body, island_net)
        l2n.connect_global(tap_substrate_isolated[index], island_net)

    for bipolar, bipolar_base, bipolar_emitter, bipolar_collector in bipolar_regions:
        # Base shares net identity with the deck's own `nwell` (`bipolar_base`
        # is always a geometric subset of it, further restricted by the
        # marker), so a base contact/pin already wired to `nwell` elsewhere in
        # this function (`well_label`, `tap`, ...) correctly names the
        # extracted base terminal's net.
        l2n.connect(bipolar_base, nwell)
        l2n.connect(bipolar_emitter, contact)
        if bipolar.collector is not None:
            l2n.connect(bipolar_collector, contact)
        else:
            l2n.connect_global(bipolar_collector, deck.substrate_net)

    # Diode terminal connectivity (issue #542), derived from each terminal's
    # declared layer rather than configured separately per entry -- see
    # `DiodeDevice`'s docstring:
    #
    # - a terminal drawn on the deck's own `nwell` shares that well's net
    #   identity (its region is always a marker-scoped subset of `nwell`),
    #   so a well tap/label elsewhere in this function names it -- the same
    #   wiring the bipolar base gets above;
    # - a substrate-formed terminal (declared `None`) joins the deck's
    #   `substrate_net` global, like the collector-less bipolar collector
    #   and `nfet_body`;
    # - any other terminal layer is a diffusion, so it joins `contact` and
    #   picks its net up from ordinary contact/metal routing -- the same
    #   wiring the bipolar emitter gets above.
    for diode, anode_region, cathode_region in diode_regions:
        for terminal_layer, terminal_region in (
            (diode.anode, anode_region),
            (diode.cathode, cathode_region),
        ):
            if terminal_layer is None:
                l2n.connect_global(terminal_region, deck.substrate_net)
            elif terminal_layer == deck.nwell:
                l2n.connect(terminal_region, nwell)
            else:
                l2n.connect(terminal_region, contact)

    try:
        l2n.extract_netlist()
    except RuntimeError as exc:
        # KLayout's own `LayoutToNetlist.extract_netlist()` raises a bare
        # `RuntimeError` (not one of *this* module's own exception types) for
        # a device whose recognised geometry leaves a terminal with no net at
        # all -- e.g. a bipolar device-mark drawn exactly coincident with its
        # emitter (`base == emitter` geometrically): `DeviceExtractorBJT3Transistor`
        # then has no base-minus-emitter extension from which to derive a
        # collector terminal, so the `bipolar.collector is None` branch above
        # connects an empty region to `deck.substrate_net` -- nothing to
        # connect, so the extracted device's `C` terminal reaches this point
        # still unconnected (issue #432). Converted to `ExtractError` here so
        # it reaches the CLI as the documented clean JSON error envelope
        # (`docs/cli/extract.md`'s "No Python traceback is printed" contract)
        # instead of an unhandled traceback.
        raise ExtractError(
            "device recognition produced a device with an unconnected "
            f"terminal ({exc}) -- this usually means a device-mark layer was "
            "drawn exactly coincident with (rather than strictly enclosing) "
            "the terminal geometry it scopes, leaving no room to derive the "
            "device's other terminal(s)"
        ) from exc
    netlist = l2n.netlist()

    # `--def-net-names` (issue #951): must run right after `l2n.netlist()` --
    # `probe_net` needs the live `l2n` -- and before anything below reads or
    # promotes net names (`_wire_abstract_cells`, `make_top_level_pins()`,
    # the purge passes). Inert unless the caller opted in.
    if def_net_names:
        def_names_renamed, def_names_unresolved = _apply_def_net_name_overrides(
            l2n, metals, def_net_name_probes
        )
        if not def_net_name_probes:
            # Loud, not silent: the opt-in found nothing to rename at all,
            # which almost always means this layout did not come from a
            # DEF->GDS merge (no shape carries property
            # `_DEF_NET_NAME_PROPERTY_ID`), so every net below is still named
            # the default text-label way.
            warnings.append(
                "--def-net-names found no DEF net-name shape property "
                f"({_DEF_NET_NAME_PROPERTY_ID}) on any routed-metal shape in "
                f"'{top_cell.name}' -- net names are unchanged (this flag "
                "expects a layout produced by a LEF/DEF -> GDS merge, e.g. "
                "`klt place-and-route`'s routed GDS)"
            )
        elif def_names_unresolved:
            joined = ", ".join(def_names_unresolved[:10])
            more = len(def_names_unresolved) - 10
            warnings.append(
                f"--def-net-names: renamed {def_names_renamed} net(s), but "
                f"{len(def_names_unresolved)} DEF net name(s) resolved to no "
                f"extracted net ({joined}"
                + (f", +{more} more" if more > 0 else "")
                + ") -- their geometry joins nothing the deck's connectivity "
                "graph sees, so those nets keep their default names"
            )

    # Diode substrate-terminal label divergence (issue #1196): runs here, not
    # earlier, because it needs both the extracted device terminals' nets and
    # a live `l2n` for `probe_net` -- and after the `--def-net-names` block
    # above so a DEF-renamed tie net is compared under its final name. A
    # no-op (no region work at all) for every deck without a substrate-formed
    # diode terminal, and for every layout whose such terminals already
    # resolved to a real drawn net.
    warnings.extend(
        _detect_diode_substrate_label_divergence(
            l2n,
            netlist.circuit_by_name(top_cell.name),
            deck,
            diode_regions,
            contact,
            poly,
            # Only the source/drain diffusion that actually touches a gate --
            # `nfet_sd`/`pfet_sd` are the deck's whole `active - poly` split,
            # so passing them undivided would subtract every diffusion shape
            # in the layout (including the drawn substrate tie being looked
            # for) rather than just recognised transistors' terminals.
            (nfet_sd + pfet_sd).interacting(poly),
        )
    )

    # `--abstract-cells` (issue #620): wire every abstracted instance in as a
    # black-box `kdb.SubCircuit` while `l2n`/`netlist` are still the *live*
    # objects `l2n.probe_net()` and `Circuit.create_subcircuit()` need, and
    # *before* `make_top_level_pins()`/the purge passes below -- connecting a
    # subcircuit pin to a net is what keeps an abstracted cell's own routing
    # stub from being purged as floating. See `_wire_abstract_cells`'s
    # docstring for the full contract.
    abstracted_cells: list[dict[str, Any]] = []
    if abstract_instances:
        top_circuit = netlist.circuit_by_name(top_cell.name)
        assert top_circuit is not None, (
            "top circuit must exist immediately after l2n.extract_netlist()"
        )
        # Metals bottom-up first, then poly/nwell/tap -- matches
        # `_probe_abstract_pin_net`'s own documented fallback order (a
        # standard cell's pins land on the lowest metal available). Getting
        # this backwards is a confirmed correctness bug (PR #622 review): a
        # parent-level well/tap shape (e.g. a guard ring) overlapping a
        # LEF-fallback pin's footprint would silently win over the metal net
        # the pin is actually routed to, since `_probe_abstract_pin_net`
        # takes the first hit. The abstracted cell's *own* nwell/poly/tap
        # cannot cause this -- `_abstract_cell_mask_layers` erases those
        # inside its definition before probing runs -- so the exposure is
        # specifically parent-level geometry.
        probe_layers: list[tuple[str, kdb.Region]] = [
            (f"metal{index}", region) for index, region in enumerate(metals)
        ] + [("poly", poly), ("nwell", nwell), ("tap", tap)]
        abstracted_cells, abstract_cell_warnings = _wire_abstract_cells(
            layout,
            deck,
            l2n,
            netlist,
            top_circuit,
            abstract_instances,
            lef_macros or {},
            probe_layers,
            abstract_cell_local_candidates,
        )
        warnings = warnings + abstract_cell_warnings

    # Flat extraction (`begin_shapes_rec`) means `make_top_level_pins()` would
    # promote *every* named net to a top-level pin -- including nets that are
    # only named because a label sits inside an instanced sub-cell, which are
    # ordinary internal nodes once instanced (issue #291). Identify those
    # below-top labels *before* promotion: strings present in the recursive
    # flatten of a label layer but never drawn directly in the top cell.
    label_layers = [deck.well_label, deck.poly_label, *deck.metal_labels]
    top_label_strings = _label_layer_strings(
        layout, top_cell, label_layers, recursive=False
    )
    all_label_strings = _label_layer_strings(
        layout, top_cell, label_layers, recursive=True
    )
    below_top_labels = all_label_strings - top_label_strings

    # Issue #1385: a layout that carries zero text on every one of `deck`'s
    # own label layers (`well_label`/`poly_label`/`metal_labels`) anywhere in
    # the whole cell tree cannot name a single net -- `make_top_level_pins()`
    # below promotes only *named* nets, so this silently zeroes out the
    # entire top-level pin interface with no error of any kind (extraction
    # itself succeeds; DRC against the same layout is unaffected). The
    # observed real-world trigger is a `klt place-and-route` request whose
    # `io.layer_h`/`io.layer_v` choice lands on a GDS layer/datatype `deck`
    # does not scan for pin-name text at all -- but this check is
    # cause-agnostic: it fires for any layout, from any source, that reaches
    # this point with no recognisable pin-name text.
    if not all_label_strings:
        scanned = ", ".join(
            f"{layer[0]}/{layer[1]}" for layer in label_layers if layer is not None
        )
        warnings.append(
            "found 0 pin-name label(s) on any of this deck's label layers "
            f"({scanned}) anywhere in '{top_cell.name}' -- no net can be "
            "named, so 0 top-level pins will be promoted below and `klt "
            "lvs` will have no net/device anchor to seed a match against a "
            "reference netlist. Compare the layers `klt layers` reports for "
            "this GDS against the list above; for a `klt place-and-route` "
            "layout in particular, check that request.io.layer_h/layer_v "
            "chose a layer this --deck actually scans for pin labels "
            "(issue #1385)"
        )

    netlist.make_top_level_pins()
    _promote_orphan_named_nets(netlist)
    demoted = _reconcile_top_pins(
        netlist, top_cell.name, below_top_labels, demote=top_cell_pins_only
    )
    if demoted:
        joined = ", ".join(demoted)
        if top_cell_pins_only:
            warnings.append(
                f"kept {len(demoted)} label-named net(s) internal: their naming "
                f"label(s) are drawn below the top cell (inside instanced "
                f"sub-cells), not in the top cell itself ({joined})"
            )
        else:
            warnings.append(
                f"promoted {len(demoted)} net(s) to top-level pins from label(s) "
                f"found only below the top cell (inside instanced sub-cells): "
                f"{joined} -- these are internal nodes once instanced; pass "
                f"top_cell_pins_only (--top-cell-pins) to keep them internal "
                f"(issue #291)"
            )

    # Issue #514: a per-*net* declared-interface reconciliation, orthogonal
    # to the per-*cell* one above. When `declared_pins` is given, demote
    # every currently-promoted pin whose net name is not in the declared
    # set -- the net keeps its name (a human/testbench can still find it),
    # it is simply not exposed as a top-level pin `combine_devices` must
    # treat as un-foldable. Applied *after* the top_cell_pins_only pass, so
    # it can only further restrict the promoted set, never re-promote a net
    # that pass already kept internal. Reuses `_reconcile_top_pins` exactly
    # as top_cell_pins_only does, with a different demote-set.
    if declared_pins is not None:
        top_circuit = netlist.circuit_by_name(top_cell.name)
        promoted_names: set[str] = set()
        if top_circuit is not None:
            for pin in top_circuit.each_pin():
                pin_net = top_circuit.net_for_pin(pin.id())
                if pin_net is not None and pin_net.name:
                    promoted_names.add(pin_net.name)

        non_declared = promoted_names - declared_pins
        demoted_by_declared_pins = _reconcile_top_pins(
            netlist, top_cell.name, non_declared, demote=True
        )
        if demoted_by_declared_pins:
            joined = ", ".join(demoted_by_declared_pins)
            warnings.append(
                f"kept {len(demoted_by_declared_pins)} net(s) internal: not in "
                f"the declared pin set (--pins / layout.declared_pins) "
                f"({joined}) -- issue #514"
            )

        unmatched_declared_pins = sorted(declared_pins - promoted_names)
        if unmatched_declared_pins:
            joined = ", ".join(unmatched_declared_pins)
            count = len(unmatched_declared_pins)
            plural = "s" if count != 1 else ""
            warnings.append(
                f"{count} declared pin name{plural} (--pins / "
                f"layout.declared_pins) matched no promoted net in the "
                f"layout: {joined}"
            )

    # Issue #1390: `def_pins`'s own DEF-merge-aware declared-pin
    # reconciliation -- the *automatic* counterpart to `declared_pins`
    # above, for a layout `klt place-and-route`'s DEF->GDS merge produced.
    # That merge flattens the whole design into the top cell, so
    # `top_cell_pins_only`'s below-top set is always empty against it (see
    # its own comment above) -- it structurally cannot distinguish a genuine
    # DEF `PINS`-section top-level port from an internal DEF `NETS`-section
    # connection point, both of which land "in the top cell" once flattened
    # (issue #1385's own documented gap). `def_pins` is that design's
    # genuine top-level port *net* names, parsed directly off the routed
    # DEF's own `PINS` section (`place_and_route.def_pin_names`).
    #
    # Plain exact-string matching (`declared_pins`'s own convention, just
    # above) does not work here: KLayout joins every distinct text label
    # found on one electrical net into a single, comma-separated `Net.name`
    # (see `spice_safe_net_name`'s docstring) -- and in a densely-routed
    # DEF-merged layout *most* nets, port or not, carry two or more such
    # labels once routing connects a driver's local output-pin label to a
    # receiver's local input-pin label (or, for a genuine port, the DEF
    # `PINS`-declared label to whichever local pin it connects into) --
    # exactly the "collided, comma-joined names" #1390's own issue text
    # describes (measured on this repo's own routed `gcd` corpus fixture:
    # 494 of 752 promoted pins carry 2+ joined labels). So a promoted net's
    # comma-joined label set is checked for *any* intersection with
    # `def_pins`, not a whole-string match -- everything else is demoted,
    # same as `declared_pins`'s plain-miss case. Applied after
    # `declared_pins`'s own pass (when both are given), so it can only
    # further restrict -- it never re-promotes a net that pass already kept
    # internal.
    if def_pins is not None:
        top_circuit = netlist.circuit_by_name(top_cell.name)
        promoted_names = set()
        if top_circuit is not None:
            for pin in top_circuit.each_pin():
                pin_net = top_circuit.net_for_pin(pin.id())
                if pin_net is not None and pin_net.name:
                    promoted_names.add(pin_net.name)

        matched_def_pins: set[str] = set()
        non_matching_def_pins: set[str] = set()
        for name in promoted_names:
            hit = set(name.split(",")) & def_pins
            if hit:
                matched_def_pins |= hit
            else:
                non_matching_def_pins.add(name)

        demoted_by_def_pins = _reconcile_top_pins(
            netlist, top_cell.name, non_matching_def_pins, demote=True
        )
        if demoted_by_def_pins:
            joined = ", ".join(demoted_by_def_pins)
            warnings.append(
                f"kept {len(demoted_by_def_pins)} net(s) internal: no drawn "
                f"label on the net matches the DEF's own declared PINS set "
                f"(--def-pins) ({joined}) -- issue #1390"
            )

        unmatched_def_pins = sorted(def_pins - matched_def_pins)
        if unmatched_def_pins:
            joined = ", ".join(unmatched_def_pins)
            count = len(unmatched_def_pins)
            plural = "s" if count != 1 else ""
            warnings.append(
                f"{count} declared DEF PINS name{plural} (--def-pins) "
                f"matched no promoted net's label set in the layout: {joined}"
            )

    # Issue #1385: the final, cause-agnostic check -- after every promotion
    # and demotion pass above (`make_top_level_pins()`, `--top-cell-pins`,
    # `--pins`/`declared_pins`, `--def-pins`, issue #1390) has run, does the
    # top circuit have *any* top-level pin left at all? A zero-pin top
    # circuit means `klt lvs`'s `NetlistComparer` has no net/device anchor to
    # seed correspondence against a reference netlist and will report a full
    # mismatch even when the two sides' device populations genuinely agree
    # -- and that failure mode gives no hint the root cause is upstream in
    # pin promotion, not device extraction. This subsumes (but does not
    # replace) the label-layer-specific warning above: it also catches an
    # otherwise-labelled layout that `--top-cell-pins`/`--pins`/`--def-pins`
    # demoted down to nothing between them.
    final_top_circuit = netlist.circuit_by_name(top_cell.name)
    if final_top_circuit is not None and final_top_circuit.pin_count() == 0:
        warnings.append(
            f"0 top-level pins are promoted on '{top_cell.name}' after "
            "extraction -- `klt lvs` has no net/device anchor to seed "
            "correspondence against a reference netlist and will report a "
            "full mismatch regardless of device-count agreement. If this "
            "design genuinely has zero top-level pins by intent, this "
            "warning can be ignored; otherwise see the pin-name-label "
            "warning above (if present) or check that "
            "--top-cell-pins/--pins/--def-pins did not demote every "
            "promoted pin (issue #1385)"
        )

    # `Netlist.purge()` (used by `_purge_preserving_named_nets` below) judges
    # a net "floating" -- and, transitively, a whole circuit/subcircuit chain
    # "unused" -- against whether it is (indirectly) connected to a real
    # `kdb.Device`, *not* against its own `pin_count()`/`subcircuit_pin_count()`
    # (verified directly against `klayout.db`: a named, pinned net whose only
    # connections are a top-level pin and a `SubCircuit` pin into a
    # device-free circuit is still wiped, along with that circuit and the
    # `SubCircuit` instance itself, exactly as if none of them had ever been
    # connected). A black-box abstraction (issue #620) is *by definition*
    # device-free, so `_purge_preserving_named_nets`'s existing rescue --
    # which only guards *individual* named/pinned nets against `purge()`,
    # not whole subcircuit chains -- is not enough once `--abstract-cells`
    # is in play: every abstracted instance (and the parent nets it is wired
    # to) would otherwise silently vanish, defeating the whole feature.
    # `abstract_instances` truthy therefore skips KLayout's native
    # `purge()`/`_purge_preserving_named_nets` entirely in favour of
    # :func:`_purge_truly_floating_nets`, a narrower, purely net-local pass
    # that removes only what is *unconditionally* junk (no pin, no device
    # terminal, no subcircuit pin -- on any circuit) and never touches a
    # circuit or subcircuit instance. The one accepted trade-off: a
    # genuinely-disconnected, unnamed junk net that `purge()` would normally
    # remove via its device-anchored definition survives when
    # `--abstract-cells` is given (it still shows up in `nets[]` with
    # `device_count: 0`) -- cosmetic noise, not a correctness gap.
    if abstract_instances:
        _purge_truly_floating_nets(netlist)
    else:
        _purge_preserving_named_nets(netlist)

    # Post-extraction device-parameter corrections (issues #512, #518, #521):
    # applied to the live `kdb.Device` objects here -- *before* the netlist is
    # handed to `NetlistSpiceWriter` (`run_extract`) or `NetlistComparer`
    # (`lvs.py`'s inline-extraction path) -- so every consumer sees the same
    # corrected value the JSON `devices[].params` report shows. See
    # `_apply_device_parameter_corrections` for the full rationale.
    #
    # The resistor `fixed_offset_ohm` term is gated by
    # `apply_resistor_fixed_offset` (issue #559): `lvs.py`'s
    # `options.combine_devices` path passes `False` here and applies it
    # itself, once, *after* combining -- see this function's docstring and
    # `apply_resistor_fixed_offset_corrections`.
    _apply_device_parameter_corrections(
        netlist, deck, apply_resistor_fixed_offset=apply_resistor_fixed_offset
    )

    # Dead metal (issue #676): routing-stack geometry left on no surviving
    # net. Like the parasitics pass below, this must run *here* -- after the
    # purge (so "the nets a caller actually sees" is the yardstick) but while
    # `l2n` still owns the shape database `polygons_of_net` reads.
    dead_metal = _detect_dead_metal(
        l2n,
        netlist.circuit_by_name(top_cell.name),
        layout,
        top_cell,
        layout.dbu,
        [
            (f"metal{index}", deck.metals[index], region, metal_index[index])
            for index, region in enumerate(metals)
        ]
        + [
            (f"via{index}", deck.vias[index], region, via_index[index])
            for index, region in enumerate(vias)
        ],
    )
    if dead_metal:
        cluster_word = "cluster" if len(dead_metal) == 1 else "clusters"
        total_shapes = sum(entry["shapes"] for entry in dead_metal)
        shape_word = "shape" if total_shapes == 1 else "shapes"
        layers_str = ", ".join(
            sorted({f"{entry['layer']}/{entry['datatype']}" for entry in dead_metal})
        )
        warnings.append(
            f"{len(dead_metal)} routing-stack {cluster_word} ({total_shapes} "
            f"{shape_word} on {layers_str}) join no extracted net -- no via "
            "lands on this geometry and no same-layer wiring touches it, so "
            "it is invisible to the extracted netlist and to every downstream "
            "`klt lvs`/`klt sim` view; deliberate dead metal (artwork, fill) "
            "is expected here, unexplained dead metal usually means the "
            "connection you intended is missing -- see dead_metal[] for the "
            "per-cluster layer/bbox/shape count."
        )

    # Parasitics geometry must be read *before* `l2n` (which owns the shape
    # database `polygons_of_net` reads) is garbage-collected below, so compute
    # it here while the graph is still live. Returned as plain data; the R/C
    # devices are injected into the netlist by `run_extract` after it has
    # already captured the schematic-equivalent `devices[]`/`nets[]` view.
    parasitic_nets: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None
    mom_crosscheck: dict[str, Any] | None = None
    if parasitics_deck is not None:
        circuit = netlist.circuit_by_name(top_cell.name)
        parasitic_nets = _compute_parasitics(
            l2n,
            circuit,
            layout.dbu,
            deck,
            parasitics_deck,
            layer_index,
            metal_index,
            critical_nets=critical_nets,
        )
        # `klt extract --mom-net <net>` (issue #798): computed here, not in
        # `run_extract`, for the same "`l2n` must still be alive" reason as
        # `parasitic_nets` immediately above -- `polygons_of_net` is a live
        # `LayoutToNetlist` API, unusable once this function returns.
        #
        # A `--mom-net` *name* can match several genuinely distinct,
        # electrically unconnected net objects -- the `gcd` corpus block has
        # 105 separate un-strapped `VGND` islands sharing one label (issue
        # #765). Take the **lowest `cluster_id`** among the matches rather
        # than whichever `each_net()` happens to yield first (issue #811):
        # that iteration order is not a documented contract (a net rescued by
        # `_purge_preserving_named_nets` is recreated at the *end* of the
        # circuit's net list while keeping its original, possibly low, id),
        # whereas `_compute_parasitics` sorts its ground list by `(net,
        # net_id)` -- so the lowest-id island is exactly the first entry a
        # caller sees for that label in `parasitics.nets[]`, and the choice
        # is reproducible run to run as `--mom-net`'s documented contract
        # requires. Which island was solved is reported back as the
        # cross-check's own `net_id`, and `run_extract` resolves the entry to
        # swap from that id rather than by name.
        if mom_net is not None and circuit is not None:
            matched_nets = [
                candidate
                for candidate in circuit.each_net()
                if candidate.cluster_id != 0
                and spice_safe_net_name(candidate.expanded_name()) == mom_net
            ]
            if matched_nets:
                matched_net = min(matched_nets, key=lambda net: net.cluster_id)
                mom_crosscheck = _mom_ground_capacitance_for_net(
                    l2n,
                    matched_net,
                    layout.dbu,
                    parasitics_deck,
                    metal_index,
                    mom_background_permittivity,
                )
                if len(matched_nets) > 1:
                    # Say so explicitly rather than silently picking one of
                    # several same-labelled islands: the reported delta is
                    # only meaningful for the island actually solved, and a
                    # caller pointing `--mom-net` at a shared power/ground
                    # label is far more likely to have meant "the net" than
                    # "this particular one of 105 islands".
                    mom_crosscheck["warnings"].append(
                        f"--mom-net '{mom_net}' matches {len(matched_nets)} "
                        "distinct, electrically unconnected nets sharing that "
                        f"layout label -- solved the one with net_id "
                        f"{matched_net.cluster_id} (the lowest, i.e. the first "
                        "entry carrying this name in parasitics.nets[]); every "
                        "other same-named net keeps its lumped-RC capacitance"
                    )

    # `l2n` (and the Region/Texts objects it owns) would otherwise be
    # garbage-collected once this function returns, which invalidates the
    # netlist it produced (KLayout raises on subsequent use) -- `dup()`
    # detaches an independently-owned copy.
    return (
        netlist.dup(),
        warnings,
        parasitic_nets,
        black_box_regions,
        dummy_devices_dropped,
        unmodelled_poly,
        abstracted_cells,
        dead_metal,
        mom_crosscheck,
    )


def _parameter_id(device_class: kdb.DeviceClass, name: str) -> int | None:
    """Return ``device_class``'s parameter id for ``name``, or ``None``.

    KLayout's ``Device.parameter(name)``/``set_parameter(name, value)``
    string overloads *raise* for a parameter the class does not define, so
    every read/write below is guarded by this lookup instead: a
    ``DeviceClassMOS4Transistor`` has no ``R``, a ``DeviceClassResistor`` has
    no ``P``, and both must be silently skipped rather than blow up
    extraction.
    """
    for param in device_class.parameter_definitions():
        if param.name == name:
            return param.id()
    return None


def apply_resistor_fixed_offset_corrections(
    netlist: kdb.Netlist, deck: ExtractionDeck
) -> None:
    """Add each opted-in resistor device class's
    :attr:`~klayout_tools.decks.ResistorDevice.fixed_offset_ohm` to ``R`` --
    once per ``kdb.Device`` object currently in ``netlist`` (issue #518,
    #559).

    Public (no leading underscore): shared between
    :func:`_apply_device_parameter_corrections`'s default apply-at-
    extraction-time call (``_extract_netlist``, when
    ``apply_resistor_fixed_offset=True``) and ``klt lvs``'s
    ``options.combine_devices`` path (``lvs.py``), which instead passes
    ``apply_resistor_fixed_offset=False`` to ``_extract_netlist`` and calls
    this function itself *after* ``Netlist.combine_devices()`` has folded
    series-connected primitives into one device object. Because this
    function walks whatever devices exist in ``netlist`` *at the time it
    runs*, calling it post-combine adds the fixed offset exactly once per
    surviving (possibly-folded) logical device, regardless of how many
    drawn primitives fed into it -- fixing the over-count KLayout's native
    series fold otherwise produces by summing each primitive's
    already-corrected ``R`` (issue #559).

    A deck that has not opted in (the default ``fixed_offset_ohm=0.0``) gets
    no write at all. Keyed by device-class *name* (``ResistorDevice.name``
    is the string KLayout reports back as ``DeviceClass.name``), so this is a
    direct lookup, not a positional match. The lookup is **case-insensitive**
    (issue #585): the in-process ``kdb.Netlist`` an inline extraction builds
    reports the deck's name verbatim (lowercase, e.g. ``res_high_po``), but a
    netlist read back from a SPICE file via ``kdb.NetlistSpiceReader`` (the
    ``layout.netlist`` pre-extracted shape in ``lvs.py``) reports every
    device-class name **uppercased** (``RES_HIGH_PO``). A verbatim lookup
    would silently miss the correction for the pre-extracted shape -- no
    error, no warning. Normalizing both sides to lowercase makes the
    post-combine correction fire identically regardless of how the netlist
    was produced. Parasitic R devices (injected by ``run_extract`` *after*
    extraction returns) carry their own generated class names and are never
    reached by this function -- no double-application.
    """
    fixed_offset_lookup = {
        resistor.name.lower(): resistor.fixed_offset_ohm
        for resistor in deck.resistors
        if resistor.fixed_offset_ohm
    }
    if not fixed_offset_lookup:
        return

    for circuit in netlist.each_circuit():
        for device in circuit.each_device():
            device_class = device.device_class()
            fixed_offset_ohm = fixed_offset_lookup.get(device_class.name.lower())
            if fixed_offset_ohm:
                r_id = _parameter_id(device_class, "R")
                if r_id is not None:
                    device.set_parameter(
                        r_id, device.parameter(r_id) + fixed_offset_ohm
                    )


def _apply_device_parameter_corrections(
    netlist: kdb.Netlist,
    deck: ExtractionDeck,
    *,
    apply_resistor_fixed_offset: bool = True,
) -> None:
    """Apply the deck's post-extraction device-parameter corrections to the
    live ``kdb.Device`` objects in ``netlist`` (issue #521).

    KLayout's device extractors compute only the single-term forms:
    ``DeviceExtractorCapacitor`` gives ``C = area_cap_f_um2 * A`` and
    ``DeviceExtractorResistor``/``...ResistorWithBulk`` gives
    ``R = L / W * sheet_rho_ohm_sq``. Two deck fields refine those into the
    two-term forms the real PDK models use:

    * :attr:`~klayout_tools.decks.CapacitorDevice.perim_cap_f_um` (issue
      #512) adds the perimeter/fringe term, so ``C`` becomes
      ``area_cap_f_um2 * A + perim_cap_f_um * P``.
    * :attr:`~klayout_tools.decks.ResistorDevice.fixed_offset_ohm` (issue
      #518) adds the fixed head/end-effect term, so ``R`` becomes
      ``L / W * sheet_rho_ohm_sq + fixed_offset_ohm`` -- applied here via
      :func:`apply_resistor_fixed_offset_corrections`, gated by
      ``apply_resistor_fixed_offset`` (issue #559, see that function's
      docstring for why the capacitor correction below needs no equivalent
      gate).

    Both corrections originally lived in :func:`_describe_devices`, which
    builds only the JSON response's ``devices[]`` array -- so the correction
    reached the report but never the ``kdb.Netlist`` itself (issue #521).
    That left the two consumers that actually matter reading the raw
    single-term value: ``run_extract``'s ``NetlistSpiceWriter`` (and
    therefore ``klt sim``, which consumes the written ``.spice``) and
    ``klt lvs``'s inline-extraction path, whose ``kdb.NetlistComparer``
    reads ``device.parameter(...)`` directly and so reported a spurious
    parameter mismatch against a reference netlist built from the PDK's real
    two-term model.

    Correcting the device object here -- once, inside
    :func:`_extract_netlist`, after ``netlist.purge()`` and before the
    netlist is returned to either consumer -- makes every downstream reader
    agree. :func:`_describe_devices` now simply reads the corrected value
    back rather than recomputing the correction itself, so the JSON
    ``devices[].params`` output is unchanged.

    A deck that has not opted in (the default ``perim_cap_f_um=0.0`` /
    ``fixed_offset_ohm=0.0`` both features were designed around) gets no
    write at all -- the netlist, the written SPICE, and the LVS comparison
    all stay bit-for-bit what they were before this correction existed.

    Corrections are keyed by device-class *name*: ``CapacitorDevice.name`` /
    ``ResistorDevice.name`` are the same strings KLayout reports back as
    ``DeviceClass.name``, so this is a direct lookup, not a positional
    match. Parasitic R/C devices (injected by ``run_extract`` *after*
    extraction returns) carry their own generated class names and are never
    reached by this function -- no double-application.

    Why ``perim_cap_f_um`` needs no ``apply_resistor_fixed_offset``-style
    gate (issue #559 asked this question of the capacitor analogue):
    KLayout's ``combine_devices()`` combines capacitors in *parallel*
    (matching two-terminal nets) by directly summing each device's raw
    parameters -- ``C``, ``A``, *and* ``P`` are each simple per-device sums
    (``dbNetlistDeviceClasses.cc``'s ``CapacitorDeviceCombiner::parallel``).
    Because ``perim_cap_f_um`` scales *with* the per-device geometric
    quantity ``P`` (unlike the resistor's constant ``fixed_offset_ohm``),
    applying it once per primitive and then summing is algebraically
    identical to summing the raw primitives first and applying it once to
    the combined totals: ``sum_i(area_i + perim_cap_f_um * P_i) ==
    sum_i(area_i) + perim_cap_f_um * sum_i(P_i)``. So the parallel-combine
    case this repo's decks actually produce (matched capacitor arrays) is
    unaffected by extraction-time application, and needs no deferral. (A
    *series*-combined capacitor pair -- rare, and not produced by any deck
    in this repo -- combines ``C`` non-linearly (harmonic mean) while still
    summing ``A``/``P`` linearly; that mismatch is a pre-existing
    approximation in KLayout's own multi-term series-capacitor combine,
    unrelated to and unaffected by whether this correction runs before or
    after combining, so it is out of scope here.)
    """
    perim_cap_lookup = {
        capacitor.name: capacitor.perim_cap_f_um
        for capacitor in deck.capacitors
        if capacitor.perim_cap_f_um
    }
    if perim_cap_lookup:
        for circuit in netlist.each_circuit():
            for device in circuit.each_device():
                device_class = device.device_class()
                perim_cap_f_um = perim_cap_lookup.get(device_class.name)
                if perim_cap_f_um:
                    c_id = _parameter_id(device_class, "C")
                    p_id = _parameter_id(device_class, "P")
                    if c_id is not None and p_id is not None:
                        device.set_parameter(
                            c_id,
                            device.parameter(c_id)
                            + perim_cap_f_um * device.parameter(p_id),
                        )

    if apply_resistor_fixed_offset:
        apply_resistor_fixed_offset_corrections(netlist, deck)


def _n_squares(area_um2: float, perimeter_um: float) -> float:
    """Estimate the number of resistive *squares* of a net's copper on one
    layer from its total area and perimeter.

    First-order geometric approximation: model the layer's shapes as one
    equivalent rectangle with the same area ``A`` and perimeter ``P``, whose
    side lengths ``L`` and ``W`` are the roots of ``t^2 - (P/2) t + A = 0``;
    the square count is then ``L / W`` (``>= 1``). This is exact for a single
    rectangular wire and reduces to ``1`` for a square. When the shapes are
    "rounder" than any rectangle allows (negative discriminant -- e.g. a
    single square-ish pad, or fragmented geometry), it clamps to ``1``.

    Deliberately simple and fixed (issue #216: "a single, fixed, first-order
    lumped model -- no fast/accurate mode selector"); it over-counts squares
    for L-shaped or multi-fragment nets, which biases the resulting series
    resistance conservatively high rather than low.
    """
    if area_um2 <= 0.0 or perimeter_um <= 0.0:
        return 0.0
    dims = equivalent_rectangle_um(area_um2, perimeter_um)
    if dims is None:
        # "Rounder" than any rectangle allows (negative discriminant -- e.g. a
        # single square-ish pad, or fragmented geometry): clamp to one square.
        return 1.0
    length, width = dims
    return max(1.0, length / width)


def _net_area_perim_um(
    l2n: kdb.LayoutToNetlist,
    net: kdb.Net,
    dbu: float,
    indices: list[int],
    subtract_indices: list[int] | None = None,
) -> tuple[float, float]:
    """Total ``(area_um2, perimeter_um)`` of ``net``'s shapes across the
    given registered layer ``indices`` (each an index returned by
    ``LayoutToNetlist.register``), with any ``subtract_indices`` layers
    geometrically removed first.

    ``subtract_indices`` lets the poly role exclude the transistor gate
    regions from a net's poly shapes before measuring (issue #226): the gate
    sits over the channel, not the substrate the coefficients describe, and
    its capacitance is already captured by the device model. The subtraction
    is a purely local operation on the per-net ``Region`` returned by
    ``polygons_of_net`` -- it registers no extra ``LayoutToNetlist`` layer, so
    the connectivity graph is untouched."""
    import klayout.db as kdb

    region = kdb.Region()
    for index in indices:
        region += l2n.polygons_of_net(net, index)
    for index in subtract_indices or ():
        region -= l2n.polygons_of_net(net, index)
    area_um2 = region.area() * dbu * dbu
    perim_um = region.perimeter() * dbu
    return area_um2, perim_um


def _bbox_overlap(a: kdb.Box, b: kdb.Box) -> bool:
    """Cheap bounding-box prefilter for the vertical-overlap coupling pass
    (issue #760): wraps ``kdb.Box.overlaps`` so the (expensive, C++-side)
    actual ``Region & Region`` boolean below only runs for net-pairs whose
    per-level bounding boxes genuinely intersect. On a routed block the vast
    majority of net-pairs on two adjacent levels do not share any XY
    footprint at all, so this alone turns an ``O(n*m)`` candidate space into
    a small fraction actually reaching the boolean AND -- no halo/neighbour
    search structure needed, matching the roadmap's own cost estimate
    (`docs/design/extract-fidelity-roadmap.md` Stage 2a: "no halo search, no
    neighbour-search structure")."""
    return a.overlaps(b)


def _net_pair_key(name_a: str, name_b: str) -> tuple[str, str]:
    """Canonical (order-independent) key for an unordered net pair, used to
    accumulate a net-to-net coupling total across every contributing
    adjacent-level pair (issue #760) -- sorted so ``(A, B)`` and ``(B, A)``
    always collide into the same accumulator entry."""
    return (name_a, name_b) if name_a <= name_b else (name_b, name_a)


def _compute_parasitics(
    l2n: kdb.LayoutToNetlist,
    circuit: kdb.Circuit | None,
    dbu: float,
    deck: ExtractionDeck,
    parasitics_deck: ParasiticsDeck,
    layer_index: dict[str, int],
    metal_index: list[int],
    critical_nets: frozenset[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute one first-order lumped ``(R, C)`` per net, plus net-to-net
    vertical-overlap coupling capacitance, from the extracted per-net/
    per-layer geometry (issue #760, Extract Stage 2a).

    For each net (except the deck's substrate/ground net, which is the AC
    ground the ground capacitances return to):

    - **C to ground** = sum over conductor roles of ``area_um2 * cap_area +
      perimeter_um * cap_perim`` (femtofarads), the lumped ground capacitance
      of the net's interconnect -- with one correction (below): a metal
      role's ``area_um2`` term has any area coupled to a *different* net on
      an adjacent level (see next) subtracted out first, so that charge
      moves from the ground term to the coupling term rather than being
      counted in both.
    - **series R** = sum over conductor roles of ``sheet_res * n_squares``
      (ohms), the net's lumped interconnect resistance. Unaffected by
      coupling: resistance is a property of the conductor's own geometry, not
      of what does or does not sit above/below it.

    Roles map to the registered geometry layers: ``poly`` the poly region with
    the transistor gate regions subtracted out (issue #226 -- gate capacitance
    lives in the device model), and ``metals[i]`` each metal-stack layer
    (index-aligned with the deck's ``metals``). The optional ``diffusion`` role
    (NMOS+PMOS source/drain) is left unset by the shipped decks: the M cards'
    ``AS``/``AD``/``PS``/``PD`` already feed the device model's junction
    capacitance, so a diffusion cap term would double-count it.

    **Vertical-overlap coupling (issue #760):** for each adjacent pair of
    metal levels ``(i, i+1)`` with a curated ``parasitics_deck.metal_overlaps``
    coefficient, and each pair of *distinct* nets with geometry on that pair
    of levels, the area of ``net_a``'s shapes on level ``i`` intersected with
    ``net_b``'s shapes on level ``i+1`` (a plain ``Region & Region`` boolean,
    KLayout's own already-registered per-net regions -- no halo/neighbour
    search) becomes a coupling capacitance between ``net_a`` and ``net_b``
    (``overlap_area_um2 * metal_overlaps[i]``), and is subtracted from
    *both* nets' ground area term on their own respective level (``net_a``'s
    on level ``i``, ``net_b``'s on level ``i+1``) -- charge is *moved*, never
    duplicated. The per-level deduction is the **geometric union** of every
    partner's overlap region with that net on that level, not the sum of
    their areas: a level participates in two adjacent pairs (as upper plate
    of ``(i-1, i)`` and lower plate of ``(i, i+1)``), so a straight via stack
    would otherwise have the same square micron deducted twice and its charge
    destroyed rather than moved. Same-net overlap (a via stack tying one net's
    own two levels together) is excluded by construction: coupling is only
    ever computed between two *distinct* nets, so a net's own via stack
    contributes nothing
    here (it is ordinary connectivity, already merged into one net upstream).
    **Lateral (same-layer, sidewall) coupling, for critical nets only (issue
    #976, Epic #709 Phase 2a):** unlike vertical-overlap coupling above,
    which is unconditional across the whole layout, this pass only ever
    considers a same-layer net pair where at least one side's name is in
    ``critical_nets`` -- ``None``/empty (the default) skips it entirely,
    byte-identical to this function's pre-#976 behaviour. This restriction
    is deliberate, not a shortcut of convenience: a full-layout lateral pass
    is the roadmap's own "medium cost" Stage 2b (a real neighbour-search
    cost across every same-layer pair on a routed block, not the cheap
    bounding-box-prefiltered ``Region & Region`` boolean vertical overlap
    uses -- see ``docs/design/extract-fidelity-roadmap.md``'s Stage 2b cost
    estimate). Scoping the search to a caller-declared "nets that matter"
    set (Epic #709 Phase 2's own framing: high-impedance nodes, a SAR ADC's
    CDAC top plate, a PLL loop filter) keeps this increment's cost bounded
    to exactly the nets a caller cares about while still closing the gap
    ``PARASITIC_MODEL_SCOPE["coupling"]`` names.

    For each metal level ``i`` with a curated
    ``parasitics_deck.metal_sidewalls[i]`` coefficient and
    ``parasitics_deck.metal_sidewall_lookback_um[i]`` lookback distance, and
    each qualifying pair of *distinct* nets with geometry on that level,
    ``net_a``'s region and ``net_b``'s region are checked with KLayout's own
    ``Region.separation_check`` (the same primitive ``drc.py`` already uses
    for ``check="separation"`` DRC rules) at that lookback distance; the
    summed length of the facing edge pairs it reports
    (``EdgePairs.first_edges().length()``) becomes a coupling capacitance
    between the two nets (``facing_length_um * metal_sidewalls[i]``). Unlike
    the vertical pass, **this charge is not deducted from either net's
    substrate fringe term** -- a known simplification (magic's own
    fringe-shielding model needs its ``defaultsidewall`` second parameter's
    semantics resolved first, an explicitly open question -- see
    ``metal_sidewall_lookback_um``'s docstring), flagged here rather than
    silently assumed away. Same-net and same-*name* pairs are skipped for
    the identical reasons the vertical pass skips them (see above).

    Returns a 2-tuple:

    - Ground list (sorted by ``(net name, net_id)`` for deterministic
      output): ``{"net", "net_id", "resistance_ohm", "capacitance_ff"}`` for
      every net with non-zero *raw* (pre-coupling-deduction) ground-eligible
      geometry --
      including a net whose ground capacitance was fully moved to coupling
      by the correction above (``capacitance_ff`` can be ``0.0`` in that
      case; the net still needs a star/hub so a coupling ``C`` card has
      somewhere to attach). A net with no eligible interconnect geometry at
      all is omitted, exactly as before this feature existed.
    - Coupled-pair list (sorted by ``(net_a, net_b)`` for deterministic
      output): ``{"net_a", "net_b", "capacitance_ff", "levels",
      "lateral_levels"}`` for every distinct net pair with non-zero
      vertical-overlap and/or (critical-net-scoped) lateral coupling --
      **one entry per pair**, even when both kinds contribute, so
      :func:`_inject_parasitics`'s "one two-terminal `C` card per pair"
      invariant holds regardless of how many geometry passes fed it.
      ``capacitance_ff`` is the pair's total coupling capacitance (vertical
      plus lateral, summed across every contributing level/level-pair);
      ``levels`` is the sorted list of ``[lower_metal_index,
      upper_metal_index]`` adjacent-level pairs that contributed vertical
      coupling (issue #760, empty if none); ``lateral_levels`` (issue #976)
      is the sorted list of same-``metal_index`` levels that contributed
      lateral coupling (empty if none, and always empty when
      ``critical_nets`` is not given -- byte-identical to this field's
      pre-#976 absence). Empty when neither ``parasitics_deck.metal_overlaps``
      nor (``critical_nets``-scoped) ``parasitics_deck.metal_sidewalls``
      curate a coefficient, or the layout has no qualifying coupling
      geometry between distinct nets.

    ``net_id`` is ``net.cluster_id`` -- the id ``LayoutToNetlist`` already
    uses internally to key a net to its layout cluster (see the
    ``cluster_id == 0`` sentinel handling below), unique across every net
    *object* in ``circuit``, unlike ``net.expanded_name()`` (issue #765: two
    genuinely distinct nets, e.g. separate un-strapped ``VGND`` islands, can
    carry the identical layout label). Callers that only care about the
    schematic-equivalent name can keep using ``net``; a caller that needs to
    distinguish same-named islands -- or :func:`_inject_parasitics`, which
    must resolve each entry back to the exact net object this function
    measured -- keys on ``net_id`` instead.

    **Pairs are keyed by SPICE-safe net *name*, not by net object**, matching
    the node the coupling ``C`` card actually lands on:
    :func:`_inject_parasitics` hangs each pair between two **per-name** hubs
    (its ``hub_by_net`` map is keyed on ``entry["net"]``), so when several
    genuinely distinct net objects share one layout label -- the ``gcd``
    corpus block has 105 separate, un-strapped ``VGND`` rail islands and 88
    ``VPWR`` ones -- every pair naming that label resolves to a single
    (last-registered) island's hub. Two consequences, both deliberate:
    overlap between two same-named nets is **skipped entirely** (its charge
    stays on ground) because both of its terminals would resolve to that one
    same hub, making the "coupling capacitor" a self-loop that would inflate
    the reported total while contributing nothing electrically; and overlap
    between two *different* names accumulates into one pair however many net
    objects on each side contributed, matching the one hub per name that the
    card attaches to.

    That name-keying is *this* pass's own aggregation choice, not something
    forced on it from downstream. Since issue #765 the ground entries above
    resolve by ``net_id``, and KLayout's ``NetlistSpiceWriter`` renames
    duplicates when it writes them (two nets both labelled ``VGND`` are
    written as the distinct nodes ``VGND`` and ``VGND$1``) -- so neither the
    injection step nor the emitted netlist collapses same-labelled islands
    into one node. Coupling is therefore modelled one level coarser than the
    per-net ground terms; a per-net-object coupling model is a named
    follow-on, not a behaviour this function claims today.
    """
    if circuit is None:
        return [], []

    import klayout.db as kdb

    # (LayerRC, [include indices], [subtract indices]) for every non-metal
    # role that has both a coefficient set and at least one present layer.
    # The subtract list is empty except for the poly role, which removes the
    # transistor gate regions (see below). Metal roles are handled separately
    # below since they participate in the vertical-overlap coupling pass.
    #
    # Every declared MOS flavour's own SD/gate registrations (issue #1111,
    # `layer_index[f"nfet_sd_{flavour}"]` etc. -- see `_extract_netlist`'s own
    # registration loop) are folded in alongside the default `nfet_sd`/
    # `pfet_sd`/`nfet_gate`/`pfet_gate` indices below, so a flavoured
    # transistor's own diffusion/gate geometry is measured (or excluded from
    # the poly role) exactly like an unflavoured one -- neither role would
    # otherwise "see" it at all, silently under-measuring a net that happens
    # to carry a flavoured device.
    flavour_sd_indices: list[int] = []
    flavour_gate_indices: list[int] = []
    for flavour in deck.mos_flavours:
        flavour_sd_indices.append(layer_index[f"nfet_sd_{flavour.flavour}"])
        flavour_sd_indices.append(layer_index[f"pfet_sd_{flavour.flavour}"])
        flavour_gate_indices.append(layer_index[f"nfet_gate_{flavour.flavour}"])
        flavour_gate_indices.append(layer_index[f"pfet_gate_{flavour.flavour}"])

    non_metal_roles: list[tuple[Any, list[int], list[int]]] = []
    if parasitics_deck.diffusion is not None:
        non_metal_roles.append(
            (
                parasitics_deck.diffusion,
                [layer_index["nfet_sd"], layer_index["pfet_sd"], *flavour_sd_indices],
                [],
            )
        )
    if parasitics_deck.poly is not None:
        # Exclude the transistor gate regions from the poly role (issue #226):
        # gate poly sits over the channel (not the substrate these coefficients
        # describe) and its capacitance is already in the device model, so the
        # nfet/pfet gate shapes are subtracted from the net's poly shapes before
        # measuring. These registrations back the device connectivity too and
        # are left untouched -- only the parasitic measurement subtracts them.
        non_metal_roles.append(
            (
                parasitics_deck.poly,
                [layer_index["poly"]],
                [
                    layer_index["nfet_gate"],
                    layer_index["pfet_gate"],
                    *flavour_gate_indices,
                ],
            )
        )

    nets: list[kdb.Net] = []
    for net in circuit.each_net():
        name = net.expanded_name()
        # Every per-isolated-region substrate identity (issue #1128, named
        # `f"{substrate_net}_iso{n}"` -- see `ExtractionDeck.
        # substrate_isolation`'s docstring) is skipped here the same way the
        # deck-wide `substrate_net` global is: each is itself a synthesized
        # local AC-ground reference for its own isolated region, not an
        # ordinary signal net whose own ground capacitance should be
        # measured. No-op when the deck declares no `substrate_isolation`
        # (no net is ever named this way).
        if name == deck.substrate_net or name.startswith(f"{deck.substrate_net}_iso"):
            continue
        if net.cluster_id == 0:
            # Belt-and-braces (issue #563): `cluster_id` is the key
            # `LayoutToNetlist` uses to find a net's shapes, and `0` is the
            # sentinel for "not tied to a layout cluster". Passing such a net
            # to `polygons_of_net` faults inside KLayout's hierarchical
            # network processor with an unhandled internal `RuntimeError`
            # rather than returning an empty region.
            # `_purge_preserving_named_nets` restores the real cluster id on
            # every net it rescues, so no net reaching here from
            # `_extract_netlist` should hit this branch -- but a net with no
            # cluster has no geometry to measure by definition, so skipping
            # it is the correct (and crash-free) answer for any future caller
            # that hands us a synthesised net.
            continue
        nets.append(net)

    # Pass 1: non-metal ground R/C (unaffected by coupling) plus, per net,
    # each metal level's raw region/area/perimeter -- cached for the
    # coupling pass below and for the final (possibly-reduced) ground C.
    base_c_ff: dict[kdb.Net, float] = {}
    base_r_ohm: dict[kdb.Net, float] = {}
    num_metals = len(parasitics_deck.metals)
    # metal_regions[i]: {net: Region} for every net with non-empty geometry
    # on metal level i -- only populated for levels with a curated LayerRC
    # (a level with none contributes no ground R/C either, matching the
    # pre-existing `metals_without_coefficient` gap semantics).
    metal_regions: list[dict[kdb.Net, kdb.Region]] = [dict() for _ in range(num_metals)]
    net_metal_area_um2: dict[tuple[kdb.Net, int], float] = {}
    net_metal_perim_um: dict[tuple[kdb.Net, int], float] = {}

    for net in nets:
        r_ohm = 0.0
        c_ff = 0.0
        for layer_rc, indices, subtract in non_metal_roles:
            area_um2, perim_um = _net_area_perim_um(l2n, net, dbu, indices, subtract)
            if area_um2 <= 0.0:
                continue
            c_ff += (
                area_um2 * layer_rc.cap_area_ff_um2
                + perim_um * layer_rc.cap_perim_ff_um
            )
            r_ohm += layer_rc.sheet_res_ohm_sq * _n_squares(area_um2, perim_um)
        base_c_ff[net] = c_ff
        base_r_ohm[net] = r_ohm

        for i in range(num_metals):
            layer_rc = parasitics_deck.metals[i]
            if layer_rc is None or i >= len(metal_index):
                continue
            region = l2n.polygons_of_net(net, metal_index[i])
            if region.is_empty():
                continue
            area_um2 = region.area() * dbu * dbu
            perim_um = region.perimeter() * dbu
            if area_um2 <= 0.0:
                continue
            net_metal_area_um2[(net, i)] = area_um2
            net_metal_perim_um[(net, i)] = perim_um
            base_r_ohm[net] += layer_rc.sheet_res_ohm_sq * _n_squares(
                area_um2, perim_um
            )
            metal_regions[i][net] = region

    # Pass 2: vertical-overlap coupling between adjacent metal levels.
    # `deduction_regions[(net, i)]` accumulates the *geometry* of `net`'s
    # level-i area that has been attributed to a coupling partner instead of
    # ground, across every adjacent-level pair that touches level `i`.
    #
    # This is a Region union, not a scalar area sum, and that distinction is
    # load-bearing (PR #764 review): level `i` participates in two adjacent
    # pairs -- as the upper plate of `(i-1, i)` and as the lower plate of
    # `(i, i+1)` -- so a straight via stack (a net with routing above *and*
    # below the same XY footprint, pervasive in any routed block) has the same
    # physical area claimed by two different partners. Summing the two overlap
    # areas would deduct that area twice and silently destroy charge; unioning
    # the overlap Regions and measuring once guarantees each square micron is
    # removed from the ground term at most once, however many partners claim
    # it. The coupling capacitors themselves are unaffected: each *pair* still
    # gets the full mutual-overlap area, which is what the plate model wants.
    deduction_regions: dict[tuple[kdb.Net, int], kdb.Region] = {}
    # `coupling_ff[pair_key]` / `coupling_levels[pair_key]`: the accumulated
    # coupling capacitance and contributing `[lower, upper]` level-index
    # pairs for one unordered net pair.
    coupling_ff: dict[tuple[str, str], float] = {}
    coupling_levels: dict[tuple[str, str], set[tuple[int, int]]] = {}

    for i in range(num_metals - 1):
        coef = (
            parasitics_deck.metal_overlaps[i]
            if i < len(parasitics_deck.metal_overlaps)
            else None
        )
        if coef is None:
            continue
        lower_nets = metal_regions[i]
        upper_nets = metal_regions[i + 1]
        if not lower_nets or not upper_nets:
            continue
        upper_items = [
            (net_b, _net_identity_name(net_b), region_b)
            for net_b, region_b in upper_nets.items()
        ]
        for net_a, region_a in lower_nets.items():
            bbox_a = region_a.bbox()
            name_a = _net_identity_name(net_a)
            for net_b, name_b, region_b in upper_items:
                if net_a is net_b:
                    # Same-net overlap (a via stack) is excluded by
                    # construction: coupling is only ever between two
                    # *distinct* nets (issue #760's explicit edge case).
                    continue
                if name_a == name_b:
                    # Two *distinct* nets that carry the same layout label
                    # (e.g. `gcd`'s 105 separate, un-strapped `VGND` rail
                    # islands). Pairs here are keyed by SPICE-safe *name*
                    # (`_net_pair_key`), and `_inject_parasitics` attaches
                    # each pair's `C` card between two per-*name* hubs (its
                    # `hub_by_net` map), so both terminals of a same-name
                    # pair would land on one and the same hub net -- a
                    # self-loop, contributing nothing electrically while
                    # inflating `total_coupling_capacitance_ff`. (That is a
                    # property of this name-keyed pair/hub model, not of the
                    # netlist: since issue #765 the *ground* entries resolve
                    # by `net_id`, and KLayout's SPICE writer renames a
                    # duplicate net on write -- `VGND` and `VGND$1` -- so the
                    # islands are not collapsed into one node downstream.)
                    # Skipped *before* the
                    # deduction below, so that area simply stays on the
                    # ground term, exactly as it did pre-#760; charge is
                    # still conserved.
                    continue
                if not _bbox_overlap(bbox_a, region_b.bbox()):
                    continue
                overlap = region_a & region_b
                if overlap.is_empty():
                    continue
                overlap_area_um2 = overlap.area() * dbu * dbu
                if overlap_area_um2 <= 0.0:
                    continue

                for ded_key in ((net_a, i), (net_b, i + 1)):
                    acc = deduction_regions.get(ded_key)
                    if acc is None:
                        acc = kdb.Region()
                        deduction_regions[ded_key] = acc
                    # `insert` appends the overlap polygons unmerged; the
                    # single `merged()` below collapses each accumulator once,
                    # which is both cheaper and exactly the set union wanted.
                    acc.insert(overlap)

                key = _net_pair_key(name_a, name_b)
                coupling_ff[key] = coupling_ff.get(key, 0.0) + overlap_area_um2 * coef
                coupling_levels.setdefault(key, set()).add((i, i + 1))

    # Pass 2b: lateral (same-layer sidewall) coupling, for a caller-declared
    # "critical nets" set only (issue #976, Epic #709 Phase 2a) -- see this
    # function's docstring for why this pass is scoped down rather than
    # running unconditionally like the vertical pass above.
    lateral_coupling_ff: dict[tuple[str, str], float] = {}
    lateral_coupling_levels: dict[tuple[str, str], set[int]] = {}

    if critical_nets:
        for i in range(num_metals):
            coef = (
                parasitics_deck.metal_sidewalls[i]
                if i < len(parasitics_deck.metal_sidewalls)
                else None
            )
            lookback_um = (
                parasitics_deck.metal_sidewall_lookback_um[i]
                if i < len(parasitics_deck.metal_sidewall_lookback_um)
                else None
            )
            if coef is None or not lookback_um or lookback_um <= 0.0:
                continue
            level_nets = metal_regions[i]
            if len(level_nets) < 2:
                continue
            lookback_dbu = max(1, round(lookback_um / dbu))
            items = [
                (net, _net_identity_name(net), region)
                for net, region in level_nets.items()
            ]
            for a_index, (net_a, name_a, region_a) in enumerate(items):
                # `critical_nets` is caller-supplied (`--critical-net`), so it
                # names nets using the same escaped spelling this module
                # reports everywhere else (issue #1162) -- `name_a`/`name_b`
                # themselves stay in the unescaped identity namespace (see
                # `_net_identity_name`'s docstring), so the membership check
                # re-escapes just for this comparison.
                a_is_critical = spice_safe_net_name(name_a) in critical_nets
                halo_bbox = region_a.bbox().enlarged(
                    kdb.Point(lookback_dbu, lookback_dbu)
                )
                for net_b, name_b, region_b in items[a_index + 1 :]:
                    if not (
                        a_is_critical or spice_safe_net_name(name_b) in critical_nets
                    ):
                        continue
                    if net_a is net_b:
                        # Same edge case the vertical pass excludes (a via
                        # stack tying one net's own geometry together is
                        # ordinary connectivity, not coupling).
                        continue
                    if name_a == name_b:
                        # Same collision-name edge case the vertical pass
                        # skips -- see its comment above.
                        continue
                    if not halo_bbox.overlaps(region_b.bbox()):
                        continue
                    edge_pairs = region_a.separation_check(region_b, lookback_dbu)
                    if edge_pairs.is_empty():
                        continue
                    facing_length_um = edge_pairs.first_edges().length() * dbu
                    if facing_length_um <= 0.0:
                        continue
                    key = _net_pair_key(name_a, name_b)
                    lateral_coupling_ff[key] = (
                        lateral_coupling_ff.get(key, 0.0) + facing_length_um * coef
                    )
                    lateral_coupling_levels.setdefault(key, set()).add(i)

    # Collapse each (net, level) accumulator to a single non-overlapping area,
    # once, now that every contributing partner has been folded in.
    deduction_um2: dict[tuple[kdb.Net, int], float] = {
        ded_key: region.merged().area() * dbu * dbu
        for ded_key, region in deduction_regions.items()
    }

    # Pass 3: finalize each net's ground C, applying the coupling deduction
    # (if any) to its metal-level area terms only -- perimeter/fringe and
    # every non-metal role are untouched by coupling.
    results: list[dict[str, Any]] = []
    for net in nets:
        raw_c_ff = base_c_ff.get(net, 0.0)
        c_ff = raw_c_ff
        for i in range(num_metals):
            layer_rc = parasitics_deck.metals[i]
            if layer_rc is None:
                continue
            area_um2 = net_metal_area_um2.get((net, i))
            if area_um2 is None:
                continue
            perim_um = net_metal_perim_um[(net, i)]
            raw_c_ff += (
                area_um2 * layer_rc.cap_area_ff_um2
                + perim_um * layer_rc.cap_perim_ff_um
            )
            deduction = deduction_um2.get((net, i), 0.0)
            effective_area_um2 = max(0.0, area_um2 - deduction)
            c_ff += (
                effective_area_um2 * layer_rc.cap_area_ff_um2
                + perim_um * layer_rc.cap_perim_ff_um
            )
        if raw_c_ff <= 0.0:
            # `raw_c_ff` is exactly the pre-#760 ground capacitance (every
            # role's full area *and* perimeter term, no coupling deduction),
            # so this net-inclusion test is bit-for-bit the one this function
            # applied before coupling existed: which nets appear in the
            # output cannot change, only how much of each net's charge sits
            # on the ground term vs. a coupling term.
            #
            # No ground-eligible geometry at all (before any coupling
            # deduction) means no load to hang a series R off of -- a bare
            # series R to nothing is meaningless, so skip the net. A net
            # whose geometry exists but was *entirely* claimed by coupling
            # still reaches here (raw_c_ff > 0), so it still gets a
            # star/hub for a coupling `C` card to attach to, even though its
            # own reported `capacitance_ff` can be `0.0`.
            continue
        results.append(
            {
                # Unescaped identity spelling (issue #1162) -- this feeds
                # `_inject_parasitics`'s real net/instance-name construction
                # below; the JSON `parasitics.nets[].net` value is derived
                # from it via `spice_safe_net_name` at the point it enters
                # the response, not baked in here.
                "net": _net_identity_name(net),
                "net_id": net.cluster_id,
                "resistance_ohm": round(base_r_ohm.get(net, 0.0), 4),
                "capacitance_ff": round(max(0.0, c_ff), 6),
            }
        )

    results.sort(key=lambda entry: (entry["net"], entry["net_id"]))

    # One entry per pair, vertical and lateral contributions merged (issue
    # #976) -- `_inject_parasitics`'s "one two-terminal `C` card per pair"
    # invariant (its own docstring) must hold regardless of how many
    # geometry passes fed a given pair, so a pair present in both
    # `coupling_ff` (vertical) and `lateral_coupling_ff` (lateral) gets one
    # combined entry, not two entries that would mint two devices under the
    # same instance name.
    coupled_pairs: list[dict[str, Any]] = []
    for key in set(coupling_ff) | set(lateral_coupling_ff):
        name_a, name_b = key
        total_ff = coupling_ff.get(key, 0.0) + lateral_coupling_ff.get(key, 0.0)
        if total_ff <= 0.0:
            continue
        levels_sorted = sorted(coupling_levels.get(key, ()))
        lateral_levels_sorted = sorted(lateral_coupling_levels.get(key, ()))
        coupled_pairs.append(
            {
                "net_a": name_a,
                "net_b": name_b,
                "capacitance_ff": round(total_ff, 6),
                "levels": [[lo, hi] for lo, hi in levels_sorted],
                "lateral_levels": list(lateral_levels_sorted),
            }
        )
    coupled_pairs.sort(key=lambda entry: (entry["net_a"], entry["net_b"]))

    return results, coupled_pairs


def _detect_dead_metal(
    l2n: kdb.LayoutToNetlist,
    circuit: kdb.Circuit | None,
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    dbu: float,
    routing_layers: list[tuple[str, tuple[int, int], kdb.Region, int]],
) -> list[dict[str, Any]]:
    """Report every routing-stack cluster that joins no extracted net (issue
    #676) -- "dead metal".

    ``routing_layers`` is one ``(role, (layer, datatype), region, index)``
    tuple per registered ``metals``/``vias`` level: ``region`` is the exact
    :class:`kdb.Region` handed to ``LayoutToNetlist.register`` (so black-box
    masking and the MiM top-via exclusion are already applied to it) and
    ``index`` is that call's returned layer index. For each level, the union
    of ``polygons_of_net(net, index)`` over every surviving net is what the
    extraction *did* account for; whatever is left after subtracting it is
    geometry no ``nets[]`` entry mentions.

    **Electrical contact, not projection.** The netted union comes from the
    extracted connectivity graph, which joins two metal levels only through a
    declared via layer -- so a wire passing under or over another wire with no
    via between them stays two nets, and an isolated shape stays dead however
    much it overlaps a live one in XY. That is the distinction a naive
    ``Region.interacting()`` check across adjacent layers gets wrong.

    **Why the surviving netlist is the yardstick.** KLayout gives *every*
    connected cluster a net at ``extract_netlist()`` time, floating ones
    included, so "has a net" is trivially true before the purge and this
    function must run after it. A floating cluster that reaches no device is
    exactly what ``Netlist.purge()`` drops, and a caller reading ``nets[]``
    can no longer find its geometry anywhere -- which is the reported
    complaint. A *labelled* floating cluster (bond pad, seal ring, power
    strap) is rescued by :func:`_purge_preserving_named_nets` with its
    ``cluster_id`` intact, so it stays netted and is deliberately **not**
    reported: a named net is findable, whatever it does or does not touch.

    Returns one entry per connected dead cluster (not per drawn polygon):
    ``{"role", "layer", "datatype", "bbox_um", "shapes", "area_um2"}``, sorted
    by ``(layer, datatype, left, bottom)``. ``role`` is ``metal<i>``/``via<i>``
    with ``<i>`` indexing the deck's own ``metals``/``vias`` tuple (``0`` is
    the bottom-most level), matching the names ``_extract_netlist`` registers
    those layers under. ``shapes`` counts the *drawn* shapes on that stream
    layer interacting with the cluster, so a human knows how much geometry to
    go look at; a cluster that is only a fragment of a drawn shape (the part
    of it left outside a black-box region, say) still counts that whole shape.
    """
    import klayout.db as kdb

    nets = (
        [net for net in circuit.each_net() if net.cluster_id != 0]
        if circuit is not None
        else []
    )

    dead_metal: list[dict[str, Any]] = []
    for role, (layer, datatype), region, index in routing_layers:
        if region.is_empty():
            continue
        netted = kdb.Region()
        for net in nets:
            netted += l2n.polygons_of_net(net, index)
        dead = region - netted
        if dead.is_empty():
            continue
        drawn = _region(layout, top_cell, (layer, datatype))
        # Raw-polygon semantics: with KLayout's default merged semantics two
        # abutting drawn boxes count as one polygon, which would report
        # "1 shape" for a cluster a human has to go edit two shapes to fix.
        drawn.merged_semantics = False
        for component in dead.merged().each():
            cluster = kdb.Region(component)
            box = component.bbox()
            dead_metal.append(
                {
                    "role": role,
                    "layer": layer,
                    "datatype": datatype,
                    "bbox_um": _bbox_um_rounded(box, dbu),
                    "shapes": drawn.interacting(cluster).count(),
                    "area_um2": round(cluster.area() * dbu * dbu, _PARAM_PRECISION_UM),
                }
            )

    dead_metal.sort(
        key=lambda entry: (
            entry["layer"],
            entry["datatype"],
            entry["bbox_um"]["left"],
            entry["bbox_um"]["bottom"],
        )
    )
    return dead_metal


def _terminal_star_positions_um(terminal_refs: list[Any]) -> list[tuple[float, float]]:
    """The approximate ``(x_um, y_um)`` location of each of ``terminal_refs``,
    read from its owning device's ``Device.trans`` (already reported in real
    micrometres -- unlike most of this module, no ``dbu`` scaling is needed).

    This is a **coarse** proxy for a terminal's true physical location
    (issue #592): KLayout's connectivity extraction records one placement
    transform per *device* (typically the center of its recognition shape),
    not a distinct position per terminal, so every terminal on the same
    device instance shares that device's single location. Good enough to
    rank a net's terminals by their approximate spread for the star-topology
    resistance split; not a substitute for true per-segment routing
    measurement (out of scope -- issue #592's deferred Option 2)."""
    positions: list[tuple[float, float]] = []
    for ref in terminal_refs:
        disp = ref.device().trans.disp
        positions.append((disp.x, disp.y))
    return positions


def _terminal_star_weights(positions: list[tuple[float, float]]) -> list[float]:
    """Normalized (sum to ``1.0``) per-terminal resistance-split weights for
    the star topology: proportional to each position's Euclidean distance
    from the centroid of ``positions``, so a terminal placed farther from a
    net's other terminals is assigned more of that net's total resistance --
    a coarse, position-aware stand-in for "farther terminals see more
    interconnect" without a full per-segment routing model (issue #592).

    Degenerates to an equal ``1/N`` split when every position coincides
    (including ``N == 1``, where the lone terminal necessarily sits at the
    "centroid") -- this is what makes a single-terminal net's star reduce to
    exactly the pre-#592 Gamma-shunt's one lumped resistor: same total value,
    not a smaller or larger one."""
    n = len(positions)
    if n == 0:
        return []
    cx = sum(p[0] for p in positions) / n
    cy = sum(p[1] for p in positions) / n
    distances = [math.hypot(x - cx, y - cy) for x, y in positions]
    total = sum(distances)
    if total <= 0.0:
        return [1.0 / n] * n
    return [d / total for d in distances]


def _distributed_rc_order(positions: list[tuple[float, float]]) -> list[int]:
    """An index permutation of ``positions`` approximating a physical tap
    order along a net's dominant spread axis (``--distributed-rc``, issue
    #977, Epic #709 Phase 2b): a ladder needs a linear chain, not an
    unordered set, so terminals must be placed in *some* sequence before
    :func:`_distributed_rc_segments` can derive adjacent-pair segment R/C.

    Finds the pair of positions with the greatest pairwise Euclidean
    distance (the same coarse "how spread out is this net" signal
    :func:`_terminal_star_weights` already leans on), projects every
    position onto the axis between that pair, and sorts by the projection
    -- a deterministic, position-only proxy for "which end is which" that
    needs no routing/skeleton geometry (:func:`_terminal_star_positions_um`'s
    own docstring: terminal position is a device-placement proxy, not true
    routed geometry).

    Degenerates to the input order (``[0, 1, ..., n - 1]``) for ``n <= 2``
    (nothing to order between two points) and whenever every position
    coincides (the projection axis is undefined) -- harmless, since every
    segment length is then ``0.0`` too and :func:`_distributed_rc_segments`'s
    equal-split fallback takes over regardless of order.
    """
    n = len(positions)
    if n <= 2:
        return list(range(n))

    best_pair = (0, 1)
    best_dist = -1.0
    for i in range(n):
        xi, yi = positions[i]
        for j in range(i + 1, n):
            xj, yj = positions[j]
            dist = math.hypot(xj - xi, yj - yi)
            if dist > best_dist:
                best_dist = dist
                best_pair = (i, j)
    if best_dist <= 0.0:
        return list(range(n))

    ai, aj = best_pair
    ax, ay = positions[ai]
    bx, by = positions[aj]
    ux, uy = bx - ax, by - ay
    norm = math.hypot(ux, uy)
    ux, uy = ux / norm, uy / norm
    projections = [
        ((x - ax) * ux + (y - ay) * uy, idx) for idx, (x, y) in enumerate(positions)
    ]
    projections.sort(key=lambda item: (item[0], item[1]))
    return [idx for _, idx in projections]


def _distributed_rc_segments(
    positions: list[tuple[float, float]],
    r_total_ohm: float,
    c_total_ff: float,
) -> tuple[list[int], list[float], list[float]]:
    """Break one net's total lumped resistance/capacitance into a chain
    ("ladder") along ``positions``' approximate physical order, for
    ``--distributed-rc`` (issue #977, Epic #709 Phase 2b) -- the multi-
    segment alternative to :func:`_inject_parasitics`'s star topology.

    Returns ``(order, segment_r_ohm, node_c_ff)``, all keyed to
    ``positions``' own indices:

    - ``order``: :func:`_distributed_rc_order`'s index permutation (length
      ``N``, one entry per terminal) -- the ladder's node sequence.
    - ``segment_r_ohm``: ``N - 1`` per-segment series resistances, one
      between each adjacent pair in ``order``. Each segment's share of
      ``r_total_ohm`` is proportional to that pair's Euclidean distance
      (its approximate physical length), so the segments always sum back
      to exactly ``r_total_ohm`` -- the same "legs sum back to the net's
      total" invariant :func:`_terminal_star_weights` already guarantees
      for the star topology.
    - ``node_c_ff``: ``N`` per-terminal ground capacitances, in ``order``'s
      sequence. Each terminal's share of ``c_total_ff`` is the *average* of
      its one or two adjacent segments' length share (an interior node
      touches two segments, an end node touches one) -- the standard "half
      the capacitance of each adjoining segment" lumped-element
      discretization of a distributed RC line. Node capacitances also sum
      back to exactly ``c_total_ff``.

    Degenerates to an equal split (``r_total_ohm / (N - 1)`` per segment,
    ``c_total_ff / N`` per node) when every position coincides -- the same
    "no spatial signal available" fallback :func:`_terminal_star_weights`
    already uses, and still conserves both totals.

    ``positions`` must have at least 2 entries -- a distributed ladder needs
    at least one segment; a net with 0 or 1 terminal has nothing to chain
    and stays on the star/Gamma-shunt path in :func:`_inject_parasitics`.
    """
    order = _distributed_rc_order(positions)
    n = len(order)
    ordered = [positions[i] for i in order]
    seg_lengths = [
        math.hypot(ordered[i + 1][0] - ordered[i][0], ordered[i + 1][1] - ordered[i][1])
        for i in range(n - 1)
    ]
    total_length = sum(seg_lengths)

    if total_length <= 0.0:
        segment_r_ohm = [r_total_ohm / (n - 1)] * (n - 1)
        node_c_ff = [c_total_ff / n] * n
        return order, segment_r_ohm, node_c_ff

    segment_r_ohm = [r_total_ohm * (length / total_length) for length in seg_lengths]
    node_c_ff = []
    for i in range(n):
        adjacent_lengths = seg_lengths[max(i - 1, 0) : min(i + 1, n - 1)]
        share = sum(adjacent_lengths) / (2.0 * total_length)
        node_c_ff.append(c_total_ff * share)
    return order, segment_r_ohm, node_c_ff


def _inject_parasitics(
    kdb: Any,
    circuit: kdb.Circuit,
    parasitic_nets: list[dict[str, Any]],
    coupled_pairs: list[dict[str, Any]],
    ground_net_name: str,
    distributed_rc_nets: frozenset[str] | None = None,
    mom_rlc_inductor: tuple[str, float] | None = None,
) -> dict[str, Any]:
    """Inject a star-topology parasitic RC per net (or a distributed
    multi-segment ladder for a caller-declared subset, see below), plus one
    two-terminal coupling capacitor per net pair with non-zero
    vertical-overlap and/or (``--critical-net``-scoped) lateral coupling
    capacitance, into ``circuit`` and return the JSON ``parasitics`` summary
    block (issue #592, extended by issues #760, #976, and #977).

    ``mom_rlc_inductor`` (``(net_name, inductance_nh)``, issue #988, Epic
    #709 Phase 3a) -- ``None`` unless ``--mom-rlc-net`` was given together
    with ``--mom-rlc-inductance-nh``, in which case, for every net named
    ``net_name`` here that ends up on the *lumped* (star/Gamma-shunt)
    model path below, one series inductor (``kdb.DeviceClassInductor``,
    henries) is spliced between that net's hub and its ground capacitor
    (``hub --L--> <fresh node> --C--> ground``) instead of the capacitor
    hanging directly off the hub. There is no inductance term anywhere in
    this function's default RC-only model for this to *replace* -- it is
    purely additive, unlike ``mom_rlc_net``'s own R/C values (substituted
    into ``parasitic_nets`` entries by the caller, before this function
    ever runs -- see ``run_extract``'s ``mom_rlc_net`` docstring paragraph).
    Never reached for a net on the *distributed* ladder path (``run_extract``
    rejects that combination up front as mutually exclusive).

    For each ``parasitic_nets`` entry **not** named in ``distributed_rc_nets``
    (the overwhelmingly common case, and the only one before issue #977), the
    net itself becomes the star's **hub**. Every device terminal that was
    connected directly to the net is moved onto a fresh per-terminal "leg"
    net, and a series resistor bridges each leg back to the hub -- so two
    terminals on the same net now sit in series through two resistors
    (``leg_a --R--> hub --R--> leg_b``), instead of sharing one node with no
    resistance between them (the pre-#592 topology this replaces). A single
    capacitor still hangs the net's total lumped ground capacitance off the
    hub (``hub --C--> <substrate_net>``), created if absent -- even when that
    capacitance rounds to ``0.0`` because every bit of it moved to coupling
    (issue #760: the hub still needs to exist for a coupling ``C`` card to
    attach to). Each leg's resistance is the net's total computed resistance
    distributed across its terminals by :func:`_terminal_star_weights` -- a
    terminal farther from the net's other connections gets more of the
    total, and the weights always sum to ``1.0`` so a net's leg resistances
    sum back to its total. A net with no device terminal at all (real
    geometry with nothing electrically attached) falls back to exactly the
    pre-#592 Gamma-shunt: one resistor from the net to a fresh internal
    node, with the capacitor on that node.

    **Distributed (multi-segment) RC ladder (``--distributed-rc``, issue
    #977, Epic #709 Phase 2b):** for a ``parasitic_nets`` entry named in
    ``distributed_rc_nets`` *and* carrying 2 or more device terminals, the
    star above is replaced by a chain: :func:`_distributed_rc_segments`
    orders the terminals along their approximate physical spread and splits
    the net's total R into ``N - 1`` series segment resistors (one between
    each adjacent ordered pair of per-terminal leg nets) and its total C into
    ``N`` per-leg ground capacitors (each leg gets its own capacitor to
    ``ground_net_name``, instead of one shared hub capacitor) -- see that
    function's docstring for the exact per-segment/per-node split. A net
    named in ``distributed_rc_nets`` with fewer than 2 device terminals (no
    chain to build) falls back to the star/Gamma-shunt path above unchanged.
    The ladder's own **hub** (the node the coupled-pair pass below attaches
    a coupling capacitor to, if this net has one) is its *middle* leg -- a
    coarse choice, since coupling geometry is not in general localized to
    one exact point on a real routed net; flagged here rather than silently
    assumed away, the same "known simplification" spirit as the lateral
    pass's own not-deducted-from-ground-fringe note in
    :func:`_compute_parasitics`'s docstring. That middle position reuses the
    original net object itself (not a fresh leg net) -- the same reason the
    star topology reuses ``net`` as its own hub: ``net`` can be a promoted
    top-level pin (or otherwise referenced by identity from outside this
    function), and every ladder position moving its terminal onto a brand
    new leg would leave ``net`` with nothing attached to it at all, silently
    orphaning that pin inside the written ``.SUBCKT`` body.

    After every ``parasitic_nets`` entry has its hub established, one
    two-terminal ``C`` card is created per ``coupled_pairs`` entry, directly
    between the two nets' **hub** nodes (not the raw net objects -- a net
    with device terminals moved its own connectivity onto leg nets, so the
    hub is the correct attachment point for anything that used to sit on the
    net itself). A pair naming a net absent from ``parasitic_nets`` (should
    not happen in practice: any net with coupling geometry has non-zero raw
    ground-eligible area by construction, see :func:`_compute_parasitics`) is
    silently skipped rather than raising, matching this function's existing
    tolerance for an unresolvable net name.

    This is purely additive from the perspective of the schematic-equivalent
    view built *before* this call (`devices[]`/`nets[]`, see `run_extract`):
    no existing device instance is removed, no pin is touched, and the
    written SPICE stays a `.SUBCKT` body directly consumable by ``klt sim``
    (every new node is internal; the subcircuit's pin interface is
    untouched). It is *not* additive to the circuit object's own internal
    wiring the way the old shunt topology was: moving a device terminal onto
    a leg net changes which SPICE node that device's `R`/`C`/`M` card names,
    even though the electrical net it represents -- and everything
    `devices[]`/`nets[]` reports about it -- is unchanged.

    The resistor and capacitor device classes are added unnamed so KLayout's
    ``NetlistSpiceWriter`` emits bare ``R``/``C`` cards with no trailing model
    token (simulator-safe).

    Resolved by ``net_id`` (``Net.cluster_id``), not by name (issue #765):
    two genuinely distinct nets can carry the identical layout label (e.g.
    separate un-strapped ``VGND`` islands nothing straps together), and
    ``net.expanded_name()`` collides across them while ``cluster_id`` does
    not -- see :func:`_compute_parasitics`'s docstring. Resolving by name
    used a last-write-wins dict, so every same-named entry silently
    resolved to whichever net object happened to be inserted last: the
    first such entry moved that net's terminals onto legs, and every
    other same-named entry found no terminals left, fell through to the
    Gamma-shunt fallback, and emitted a device instance name derived from
    the same (colliding) net string -- duplicate SPICE instance names, and
    R/C computed for one island silently attached to a different island's
    terminals. A per-entry ``instance_name`` counter below keeps device
    instance names unique even now that every entry resolves to its own,
    correct net object.
    """
    res_class = kdb.DeviceClassResistor()
    cap_class = kdb.DeviceClassCapacitor()
    ind_class = kdb.DeviceClassInductor()
    netlist = circuit.netlist()
    netlist.add(res_class)
    netlist.add(cap_class)
    netlist.add(ind_class)

    ground = circuit.net_by_name(ground_net_name)
    if ground is None:
        ground = circuit.create_net(ground_net_name)

    # Keyed by `cluster_id`, not by name (issue #765): `cluster_id` is
    # unique per net *object* within a circuit (it is the id
    # `LayoutToNetlist` itself uses to tie a net to its layout cluster),
    # while `expanded_name()` is not -- two distinct nets can carry the
    # same layout label. `existing_names` (used below purely for
    # collision-avoidance when minting fresh leg/hub net *names*) still
    # needs the set of every current net name, so it is built separately via
    # `_net_identity_name` (issue #696) exactly as before -- the *unescaped*
    # namespace fresh leg/hub nets are actually minted into (issue #1162;
    # see `_net_identity_name`'s docstring for why baking the escaped
    # spelling in here would double-escape the written netlist).
    nets_by_id = {net.cluster_id: net for net in circuit.each_net()}
    existing_names = {_net_identity_name(net) for net in circuit.each_net()}

    # Tracks how many entries so far have sanitized to a given base
    # instance name (issue #765): two entries can share a `net` string
    # (same layout label, distinct net objects), and `_sanitize_instance_name`
    # is a pure function of that string, so without this counter their
    # emitted `R`/`C` device instance names would collide even though each
    # entry now resolves to its own correct net object. The first entry to
    # reach a given base name keeps it unsuffixed (no behavior change for
    # the overwhelmingly common non-colliding case); every subsequent one
    # gets a `_dup<n>` suffix.
    instance_name_counts: dict[str, int] = {}

    # Per-net `coupled[]` view built from `coupled_pairs` before the main
    # loop below, so each `report_nets` entry can carry its own counterpart
    # list directly (issue #760).
    coupled_by_net: dict[str, list[dict[str, Any]]] = {}
    for pair in coupled_pairs:
        for this_net, other_net in (
            (pair["net_a"], pair["net_b"]),
            (pair["net_b"], pair["net_a"]),
        ):
            coupled_by_net.setdefault(this_net, []).append(
                {
                    # Unescaped identity spelling (issue #1162), matching
                    # `this_net`'s own namespace so sorting below stays
                    # stable -- escaped to the netlist's own spelling only
                    # at the `report_nets` construction site further down,
                    # via `spice_safe_net_name`.
                    "net": other_net,
                    "capacitance_ff": pair["capacitance_ff"],
                    "levels": pair["levels"],
                    # Additive field (issue #976): same-layer levels that
                    # contributed lateral coupling to this pair -- see
                    # `_compute_parasitics`'s docstring. Always `[]` unless
                    # `--critical-net` named one side of this pair.
                    "lateral_levels": pair["lateral_levels"],
                }
            )
    for entries in coupled_by_net.values():
        entries.sort(key=lambda entry: entry["net"])

    report_nets: list[dict[str, Any]] = []
    hub_by_net: dict[str, kdb.Net] = {}
    total_r = 0.0
    total_c_ff = 0.0
    total_r_count = 0
    # Actual capacitor *device* count (issue #977): 1 per lumped
    # (star/Gamma-shunt) net, `N` per distributed net's `N` per-leg
    # capacitors -- unlike `len(report_nets)` (one *entry* per net
    # regardless of model), this always equals the number of `C` cards the
    # written SPICE netlist actually carries.
    total_c_count = 0
    # `mom_rlc_inductor` series-inductor device count/total (issue #988) --
    # 0/0.0 unless `mom_rlc_inductor` was given, same "always present,
    # 0-when-unused" convention `total_coupling_capacitance_ff` already
    # follows.
    total_l_count = 0
    total_l_nh = 0.0
    for entry in parasitic_nets:
        net = nets_by_id.get(entry["net_id"])
        if net is None:
            continue

        base_instance_name = _sanitize_instance_name(entry["net"])
        dup_count = instance_name_counts.get(base_instance_name, 0)
        instance_name_counts[base_instance_name] = dup_count + 1
        instance_name = (
            base_instance_name
            if dup_count == 0
            else f"{base_instance_name}_dup{dup_count}"
        )
        r_total_ohm = max(entry["resistance_ohm"], _MIN_PARASITIC_R_OHM)
        c_farad = entry["capacitance_ff"] * 1e-15

        # Snapshot before mutating: moving a terminal off `net` below changes
        # what `net.each_terminal()` would yield mid-iteration.
        terminal_refs = list(net.each_terminal())

        terminal_reports: list[dict[str, Any]] = []
        segment_reports: list[dict[str, Any]] = []
        rc_model = "lumped"
        net_c_count = 0
        if (
            distributed_rc_nets is not None
            # `distributed_rc_nets` is caller-supplied (`--critical-net`
            # naming a `--distributed-rc` target): re-escaped for the
            # comparison, same rationale as the lateral-coupling pass above
            # (issue #1162).
            and spice_safe_net_name(entry["net"]) in distributed_rc_nets
            and len(terminal_refs) >= 2
        ):
            # Distributed (multi-segment) RC ladder (issue #977, Epic #709
            # Phase 2b) -- see this function's docstring and
            # `_distributed_rc_segments`'s own docstring for the exact
            # per-segment/per-node derivation.
            rc_model = "distributed"
            positions = _terminal_star_positions_um(terminal_refs)
            order, segment_r_ohm, node_c_ff = _distributed_rc_segments(
                positions, r_total_ohm, entry["capacitance_ff"]
            )

            # The *middle* ladder position reuses the original `net` object
            # itself, exactly like the star topology's own `hub = net` above
            # -- not a fresh net. `net` may be a promoted top-level pin (or
            # otherwise referenced by identity from outside this function);
            # every *other* position already moves its terminal onto a fresh
            # leg net the same way the star topology does, but if *every*
            # position did that here, `net` itself would end this function
            # with no device, resistor, or capacitor attached to it at all --
            # silently orphaning that pin inside the written `.SUBCKT` body.
            # Reusing `net` at exactly one position (this function's own
            # coupling-attachment point, "the middle leg" per this function's
            # docstring) keeps that position's identity intact for free,
            # mirroring the star's own reuse.
            mid_index = len(order) // 2

            legs: list[kdb.Net] = [net] * len(order)
            leg_names: list[str] = [entry["net"]] * len(order)
            for position_in_order, terminal_index in enumerate(order):
                term_ref = terminal_refs[terminal_index]
                device = term_ref.device()
                terminal_def = term_ref.terminal_def()

                if position_in_order == mid_index:
                    leg = net
                    leg_name = entry["net"]
                    # Already connected to `net` -- nothing to move.
                else:
                    leg_name = _unique_net_name(
                        entry["net"], existing_names, suffix=f"__t{terminal_index}"
                    )
                    existing_names.add(leg_name)
                    leg = circuit.create_net(leg_name)
                    device.disconnect_terminal(terminal_def.id())
                    device.connect_terminal(terminal_def.id(), leg)

                legs[position_in_order] = leg
                leg_names[position_in_order] = leg_name

                node_c_farad = node_c_ff[position_in_order] * 1e-15
                node_cap = circuit.create_device(
                    cap_class, f"{instance_name}_n{position_in_order}"
                )
                node_cap.connect_terminal("A", leg)
                node_cap.connect_terminal("B", ground)
                node_cap.set_parameter("C", node_c_farad)
                net_c_count += 1

                terminal_reports.append(
                    {
                        "device": device.expanded_name(),
                        "terminal": terminal_def.name,
                        # Report-boundary escape (issue #1162): `leg_name`
                        # is this leg's real, unescaped net identity (see
                        # `_net_identity_name`'s docstring); `spice_safe_
                        # net_name` makes this JSON value byte-identical to
                        # the written netlist's own node spelling for it.
                        "leg_net": spice_safe_net_name(leg_name),
                        "order": position_in_order,
                        "capacitance_ff": round(node_c_ff[position_in_order], 6),
                    }
                )

            for seg_index, seg_r_ohm in enumerate(segment_r_ohm):
                seg_r_ohm_clamped = max(seg_r_ohm, _MIN_PARASITIC_R_OHM)
                r_dev = circuit.create_device(
                    res_class, f"{instance_name}_seg{seg_index}"
                )
                r_dev.connect_terminal("A", legs[seg_index])
                r_dev.connect_terminal("B", legs[seg_index + 1])
                r_dev.set_parameter("R", seg_r_ohm_clamped)
                total_r_count += 1
                segment_reports.append(
                    {
                        # Report-boundary escape (issue #1162), same
                        # rationale as `terminal_reports[].leg_net` above.
                        "net_a": spice_safe_net_name(leg_names[seg_index]),
                        "net_b": spice_safe_net_name(leg_names[seg_index + 1]),
                        "resistance_ohm": round(seg_r_ohm_clamped, 4),
                    }
                )

            # The coupled-pair pass below attaches to the ladder's *middle*
            # leg -- see this function's docstring's "known simplification"
            # note. It is `net` itself (see above), so `hub_name` here is
            # `entry["net"]`, exactly the star topology's own convention.
            hub = legs[mid_index]
            hub_name = leg_names[mid_index]
        elif terminal_refs:
            hub = net
            hub_name = entry["net"]
            positions = _terminal_star_positions_um(terminal_refs)
            weights = _terminal_star_weights(positions)
            for i, (term_ref, weight) in enumerate(
                zip(terminal_refs, weights, strict=True)
            ):
                leg_name = _unique_net_name(
                    entry["net"], existing_names, suffix=f"__t{i}"
                )
                existing_names.add(leg_name)
                leg = circuit.create_net(leg_name)

                device = term_ref.device()
                terminal_def = term_ref.terminal_def()
                device.disconnect_terminal(terminal_def.id())
                device.connect_terminal(terminal_def.id(), leg)

                leg_r_ohm = max(r_total_ohm * weight, _MIN_PARASITIC_R_OHM)
                r_dev = circuit.create_device(res_class, f"{instance_name}_t{i}")
                r_dev.connect_terminal("A", leg)
                r_dev.connect_terminal("B", hub)
                r_dev.set_parameter("R", leg_r_ohm)
                total_r_count += 1

                terminal_reports.append(
                    {
                        "device": device.expanded_name(),
                        "terminal": terminal_def.name,
                        # Report-boundary escape (issue #1162), same
                        # rationale as the distributed-model case above.
                        "leg_net": spice_safe_net_name(leg_name),
                        "resistance_ohm": round(leg_r_ohm, 4),
                    }
                )
        else:
            # No device terminal to fan a star out to (e.g. real routed
            # geometry with nothing electrically attached) -- fall back to
            # the pre-#592 Gamma-shunt so the net's capacitance still has
            # somewhere to attach.
            hub_name = _unique_net_name(entry["net"], existing_names)
            existing_names.add(hub_name)
            hub = circuit.create_net(hub_name)

            r_dev = circuit.create_device(res_class, instance_name)
            r_dev.connect_terminal("A", net)
            r_dev.connect_terminal("B", hub)
            r_dev.set_parameter("R", r_total_ohm)
            total_r_count += 1

        net_inductance_nh = 0.0
        if rc_model == "lumped":
            # `--mom-rlc-net`/`--mom-rlc-inductance-nh` (issue #988, Epic
            # #709 Phase 3a): splice one series inductor between the hub and
            # the ground capacitor for exactly the net `mom_rlc_inductor`
            # names -- see this function's own `mom_rlc_inductor` docstring
            # paragraph. `c_ground_node` is the capacitor's own "A" terminal
            # below: `hub` unless this splice applies, in which case it is
            # the fresh node between the inductor and the capacitor.
            c_ground_node = hub
            # `mom_rlc_inductor[0]` is caller-supplied (`--mom-rlc-net`):
            # re-escaped for the comparison, same rationale as
            # `distributed_rc_nets` above (issue #1162).
            if (
                mom_rlc_inductor is not None
                and spice_safe_net_name(entry["net"]) == mom_rlc_inductor[0]
            ):
                net_inductance_nh = mom_rlc_inductor[1]
                l_node_name = _unique_net_name(
                    entry["net"], existing_names, suffix="__l"
                )
                existing_names.add(l_node_name)
                l_node = circuit.create_net(l_node_name)
                l_dev = circuit.create_device(ind_class, f"{instance_name}_l")
                l_dev.connect_terminal("A", hub)
                l_dev.connect_terminal("B", l_node)
                l_dev.set_parameter("L", net_inductance_nh * 1e-9)
                c_ground_node = l_node
                total_l_count += 1
                total_l_nh += net_inductance_nh
            # The distributed ladder above already created one capacitor per
            # leg (summing back to the net's total, see
            # `_distributed_rc_segments`'s docstring) -- creating a second,
            # hub-level capacitor here would double-count the net's
            # capacitance.
            c_dev = circuit.create_device(cap_class, instance_name)
            c_dev.connect_terminal("A", c_ground_node)
            c_dev.connect_terminal("B", ground)
            c_dev.set_parameter("C", c_farad)
            net_c_count = 1

        hub_by_net[entry["net"]] = hub
        total_r += r_total_ohm
        total_c_ff += entry["capacitance_ff"]
        total_c_count += net_c_count
        report_nets.append(
            {
                # Report-boundary escape (issue #1162): `entry["net"]` is
                # this net's unescaped identity spelling (see
                # `_net_identity_name`'s docstring); `spice_safe_net_name`
                # makes this JSON value byte-identical to the written
                # netlist's own node spelling for it.
                "net": spice_safe_net_name(entry["net"]),
                # Additive field (issue #765): disambiguates entries whose
                # `net` string collides across distinct net objects (e.g.
                # separate un-strapped `VGND` islands) -- see
                # `_compute_parasitics`'s docstring. `net` itself is
                # unchanged, so this is not a breaking schema change.
                "net_id": entry["net_id"],
                "resistance_ohm": entry["resistance_ohm"],
                "capacitance_ff": entry["capacitance_ff"],
                # Additive field (issue #988): the series inductor spliced in
                # for this net by `mom_rlc_inductor` -- `0.0` (the default)
                # for every net unless `--mom-rlc-net`/
                # `--mom-rlc-inductance-nh` named this one.
                "inductance_nh": net_inductance_nh,
                "hub_net": spice_safe_net_name(hub_name),
                # Additive field (issue #977): `"lumped"` (the pre-#977
                # star/Gamma-shunt model, always this value unless
                # `--distributed-rc` named this net) or `"distributed"` (the
                # multi-segment ladder above).
                "rc_model": rc_model,
                "terminals": terminal_reports,
                # Additive field (issue #977): the ladder's per-segment
                # resistors, in `order` sequence -- `[]` unless
                # `rc_model == "distributed"`.
                "segments": segment_reports,
                "coupled": [
                    {**c, "net": spice_safe_net_name(c["net"])}
                    for c in coupled_by_net.get(entry["net"], [])
                ],
            }
        )

    # One two-terminal `C` card per coupled net pair, between the two nets'
    # **hub** nodes -- built only after every entry above has established its
    # hub, since a pair can name either side in either order (issue #760).
    total_cc_ff = 0.0
    cc_count = 0
    for pair in coupled_pairs:
        hub_a = hub_by_net.get(pair["net_a"])
        hub_b = hub_by_net.get(pair["net_b"])
        if hub_a is None or hub_b is None:
            # Should not happen (see docstring): a net with coupling
            # geometry always has non-zero raw ground-eligible area, so it
            # always reaches `parasitic_nets` and gets a hub. Skipped rather
            # than raised, matching this function's existing tolerance for
            # an unresolvable net name a few lines up.
            continue
        cc_instance_name = _sanitize_instance_name(f"{pair['net_a']}_{pair['net_b']}")
        cc_dev = circuit.create_device(cap_class, f"cc_{cc_instance_name}")
        cc_dev.connect_terminal("A", hub_a)
        cc_dev.connect_terminal("B", hub_b)
        cc_dev.set_parameter("C", pair["capacitance_ff"] * 1e-15)
        total_cc_ff += pair["capacitance_ff"]
        cc_count += 1

    # DC reference for the deck's synthesized substrate identities (issue
    # #1263) -- last, so it sees every net this function created and cannot
    # collide with one of them.
    substrate_dc_tie = _tie_substrate_nets_to_ground(
        circuit, res_class, ground_net_name
    )

    return {
        "r_count": total_r_count,
        "c_count": total_c_count,
        "cc_count": cc_count,
        # Additive fields (issue #988): `mom_rlc_inductor`'s series-inductor
        # device count/total -- `0`/`0.0` (the default) unless
        # `--mom-rlc-net`/`--mom-rlc-inductance-nh` was given.
        "l_count": total_l_count,
        "total_resistance_ohm": round(total_r, 4),
        "total_capacitance_ff": round(total_c_ff, 6),
        "total_coupling_capacitance_ff": round(total_cc_ff, 6),
        "total_inductance_nh": round(total_l_nh, 6),
        "nets": report_nets,
        # Additive field (issue #1263): the substrate DC-reference shunt(s)
        # written into the `.SUBCKT` body -- see
        # `_tie_substrate_nets_to_ground`'s docstring.
        "substrate_dc_tie": substrate_dc_tie,
    }


def _tie_substrate_nets_to_ground(
    circuit: kdb.Circuit,
    res_class: kdb.DeviceClass,
    substrate_net: str,
) -> dict[str, Any]:
    """Give every *synthesized* substrate identity in ``circuit`` a DC path
    to SPICE's global ground node ``0``, via one large shunt resistor per net
    (``R<net>_dctie <net> 0 1e12``), and report what was written (issue
    #1263).

    **Why this is needed.** ``--parasitics`` hangs each net's lumped ground
    capacitance off the deck's ``substrate_net`` (``vsubs`` for sky130 and
    gf180mcu), and ``connect_global`` mints that net -- plus any
    per-isolated-region ``f"{substrate_net}_iso{n}"`` variant (issue #1128)
    -- out of nothing: no layout can draw a label for it. Nothing in the
    written netlist then gives it a defined DC value. A caller who
    ``.include``s the extracted file and ``X``-instantiates its ``.SUBCKT``
    -- the exact convention ``docs/cli/extract.md`` documents -- therefore
    hands ngspice a node whose only connections are capacitors, and the
    ``.op``/transient solve hits ``Warning: singular matrix: check node
    x<dut>.vsubs`` on it. The reported ngspice recovery (dynamic gmin
    stepping, then true gmin stepping, then source stepping) can still
    return *a* number, but not a reproducible one -- so the failure mode is
    a silently untrustworthy post-layout result, not a hard error.

    Being a *pin* does not save it: ``make_top_level_pins()`` promotes the
    substrate net like any other named net, but a promoted pin wired to an
    equally-undriven node in the caller's testbench is just as floating. The
    missing DC tie, not the pin-exposure status, is the defect.

    **Why a shunt resistor to node ``0``, inside the ``.SUBCKT``.** Node
    ``0`` is SPICE's *global* ground: it means the same node inside a
    subcircuit body as at the top level, needing neither a ``.global``
    declaration (which is conventionally a top-level card, not a
    ``.SUBCKT``-body one) nor any cooperation from the instantiating
    testbench. So the tie travels with the extracted file itself and works
    identically for a caller who runs ``klt extract --parasitics`` and
    assembles their own testbench, for ``klt pex``'s orchestration, and for
    anything downstream that re-includes the same artifact.

    **Idempotent with a hand-authored tie.** A testbench that already
    supplies its own ``.global vsubs`` + ``Vsubs vsubs 0 DC 0`` (or
    ``.options rshunt=1e12``) keeps working unchanged: a 1 Tohm resistor in
    parallel with an ideal 0 V source draws ~0 A and moves no node voltage
    -- there is no duplicate instance name to clash, because this card lives
    in the subcircuit's own namespace. Existing fixtures that hand-author
    the workaround are left in place for exactly that reason.

    **Harmless where nothing floats.** On a net that already has a real DC
    path, adding a 1 Tohm leakage path to ground is below every simulator
    tolerance -- the same property that makes ngspice's own blanket
    ``.options rshunt`` safe (the reporter measured byte-identical results
    with and without it on a non-extracted leg).

    **Pin interface untouched.** No pin is created, promoted, or demoted
    here: net ``0`` is minted after ``make_top_level_pins()``/``_reconcile_
    top_pins`` have already run, so it stays internal and the written
    ``.SUBCKT``'s declared pin count is unchanged -- which is what keeps
    ``klt pex``'s ``pin_count_mismatch``/``flat_dut_mismatch`` diagnostics
    (issue #1258) reading the same interface they did before.

    Returns the additive ``parasitics.substrate_dc_tie`` report block:
    ``{"resistance_ohm": float, "nets": [{"net": str, "device": str}, ...]}``
    -- ``nets`` empty (and no card written) when this circuit carries no
    synthesized substrate identity at all.
    """
    tied_nets = [
        net
        for net in circuit.each_net()
        if _is_synthesized_substrate_net_name(_net_identity_name(net), substrate_net)
    ]
    entries: list[dict[str, str]] = []
    if tied_nets:
        reference = circuit.net_by_name(_SPICE_GLOBAL_GROUND_NODE)
        if reference is None:
            reference = circuit.create_net(_SPICE_GLOBAL_GROUND_NODE)
        existing_devices = {device.expanded_name() for device in circuit.each_device()}
        for net in sorted(tied_nets, key=_net_identity_name):
            identity = _net_identity_name(net)
            instance_name = _unique_net_name(
                _sanitize_instance_name(identity), existing_devices, suffix="_dctie"
            )
            existing_devices.add(instance_name)
            tie = circuit.create_device(res_class, instance_name)
            tie.connect_terminal("A", net)
            tie.connect_terminal("B", reference)
            tie.set_parameter("R", SUBSTRATE_DC_TIE_RESISTANCE_OHM)
            entries.append(
                {
                    # Report-boundary escape (issue #1162), so this JSON
                    # value is byte-identical to the netlist's own node
                    # spelling -- same convention as `nets[].net` above.
                    "net": spice_safe_net_name(identity),
                    "device": f"R{instance_name}",
                }
            )
    return {
        "resistance_ohm": SUBSTRATE_DC_TIE_RESISTANCE_OHM,
        "nets": entries,
    }


def spice_safe_net_name(name: str) -> str:
    """Rewrite a KLayout ``Net.expanded_name()`` string to the exact spelling
    KLayout's own ``NetlistSpiceWriter`` writes for that net's *node*
    references in the ``.SUBCKT``/instance lines of the written SPICE file
    (issue #696, issue #1162).

    Two independent rewrites, both mirroring escaping ``NetlistSpiceWriter``
    already applies when it writes a net as a node reference (as opposed to
    the raw form it keeps in its own leading ``* pin ...``/``* net ...``
    comments):

    1. **Merged labels (issue #696).** ``Net.expanded_name()`` joins every
       distinct text label found on one electrical net with ``,`` (see
       :func:`_detect_merged_net_labels`'s docstring, issue #470) -- but a
       SPICE node token cannot contain a comma (a common argument
       separator), so ``NetlistSpiceWriter`` writes the *same* joined net
       using ``|`` instead wherever it appears as an actual node reference.
    2. **Anonymous nets (issue #1162).** A net with no drawn label at all
       gets KLayout's auto-generated ``$<n>`` placeholder as its
       ``expanded_name()`` (e.g. ``$2``) -- but ngspice (and the wider
       SPICE3/HSPICE-descended dialect family) treats a token that *starts*
       with ``$`` as an inline-comment marker, silently truncating the rest
       of the card. ``NetlistSpiceWriter`` backslash-escapes a leading ``$``
       (``\\$2``, confirmed against a live ``NetlistSpiceWriter`` run: a
       node named ``$weird`` writes as ``\\$weird``, while a *mid-token*
       ``$`` such as ``mid$dle`` is left alone -- only the leading
       character triggers the ngspice comment hazard) wherever it appears
       as a node reference; this function does the same.

    Before this function existed (for case 1) and before issue #1162 (for
    case 2), every net name this module put into the JSON response
    (``nets[].name``, ``devices[].nets[...]``, ``merged_net_labels[].net``,
    ``parasitics.nets[].net``, ``parasitics.nets[].terminals[].leg_net``)
    used the *unescaped* form, while the written netlist used the escaped
    form -- the same net, spelled two different ways depending which
    artifact you read it from, with no way for a caller to know the two
    strings named the same node short of hard-coding the substitutions
    themselves (and, for the anonymous-net case, a caller that copied the
    unescaped JSON spelling verbatim into a hand-authored SPICE card would
    reproduce the very comment-truncation hazard ``NetlistSpiceWriter``
    itself already avoids). Calling this on every net name before it enters
    the response makes it byte-identical to the netlist's own spelling
    everywhere it is reported (also applied by ``klt lvs``'s
    ``net_correspondence``/``mismatches[].net`` via ``lvs.py``'s
    ``_name_or_none``, sourced from the same ``Net.expanded_name()``
    convention).

    A no-op for the overwhelming majority of net names, which contain
    neither a comma nor a leading ``$``.
    """
    escaped = name.replace(",", "|")
    if escaped.startswith("$"):
        escaped = "\\" + escaped
    return escaped


def _net_identity_name(net: kdb.Net) -> str:
    """The comma -> ``|`` (issue #696) rewrite of ``net.expanded_name()``
    *without* :func:`spice_safe_net_name`'s leading-``$`` backslash escape
    (issue #1162) -- used only where the resulting string becomes (part of)
    the *real* name of a ``kdb.Net``/``kdb.Device`` this module creates in
    the working circuit (``_compute_parasitics``'s internal coupling-pair
    keys and ground-net list, and everything derived from them inside
    ``_inject_parasitics``: ``_unique_net_name``'s collision-avoidance set,
    the actual leg/hub nets ``circuit.create_net`` mints, and
    ``_sanitize_instance_name``'s input).

    Baking the *already-escaped* (``\\$2``-style) spelling into a real net's
    name would double-escape it: ``NetlistSpiceWriter`` applies its own
    leading-``$``/backslash escaping when it writes a net as a node
    reference, so a net whose actual name already starts with a literal
    backslash comes out with *two* backslashes in the written netlist
    (confirmed directly against a live ``NetlistSpiceWriter`` run). Net
    identity, instance-name sanitization, and CLI net-name matching
    (``--critical-net``, ``--mom-rlc-net``) all stay in this *unescaped*
    namespace -- exactly the pre-#1162 ``spice_safe_net_name`` behavior --
    so a leading ``$`` continues to compare/collide the same way it always
    has. Only the JSON *report* value derived from a name in this namespace
    (built by re-running it through :func:`spice_safe_net_name` at the point
    it enters a response field, e.g. ``parasitics.nets[].net``/``hub_net``/
    ``terminals[].leg_net``) picks up the escape, matching the netlist's own
    spelling without touching what the underlying net is actually called.
    """
    return net.expanded_name().replace(",", "|")


def _unique_net_name(base: str, existing: set[str], suffix: str = "__par") -> str:
    """A SPICE-safe internal parasitic-node name derived from ``base`` that
    does not collide with any already-present net name (an underscore suffix,
    not a dot, so ngspice never mistakes it for a hierarchy separator).

    ``suffix`` defaults to the original ``__par`` shunt-node suffix (issue
    #216/#283); the star topology (issue #592) also derives per-terminal
    "leg" net names from this same collision-avoidance logic with a
    ``__t<i>``-style suffix."""
    candidate = f"{base}{suffix}"
    if candidate not in existing:
        return candidate
    counter = 2
    while f"{base}{suffix}{counter}" in existing:
        counter += 1
    return f"{base}{suffix}{counter}"


def _describe_devices(
    circuit: kdb.Circuit,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build the response's ``devices[]`` array and ``device_counts`` map.

    Every reported parameter is read straight off the ``kdb.Device`` object
    -- including ``c_f`` and ``r_ohm``, whose deck-declared two-term
    corrections (``CapacitorDevice.perim_cap_f_um``, issue #512, and
    ``ResistorDevice.fixed_offset_ohm``, issue #518) have already been
    applied to the device itself by
    :func:`_apply_device_parameter_corrections` inside
    :func:`_extract_netlist`. Those corrections used to be computed *here*,
    into the returned dict only, which left the written SPICE netlist and
    ``klt lvs``'s ``NetlistComparer`` reading the uncorrected value (issue
    #521); reading the already-corrected device back keeps this function's
    output identical while making it a report of the netlist rather than a
    second, independent computation.
    """
    devices: list[dict[str, Any]] = []
    device_counts: dict[str, int] = {}

    for device in circuit.each_device():
        device_class = device.device_class()
        class_name = device_class.name

        nets: dict[str, str | None] = {}
        for terminal in device_class.terminal_definitions():
            net = device.net_for_terminal(terminal.id())
            nets[terminal.name.lower()] = (
                spice_safe_net_name(net.expanded_name()) if net is not None else None
            )

        params: dict[str, float] = {}
        for param in device_class.parameter_definitions():
            if param.name == "W":
                params["w_um"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "L":
                params["l_um"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "C":
                # Drawn-capacitor device classes only (#225): KLayout's
                # `DeviceClassCapacitor` reports capacitance in farads. MOS/
                # bipolar classes have no `C` parameter, so this branch never
                # fires for them. Already carries the deck's
                # `perim_cap_f_um * P` perimeter/fringe term when the deck
                # opted in (issue #512), applied at full precision to the
                # device itself by `_apply_device_parameter_corrections`.
                params["c_f"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_FARAD
                )
            elif param.name == "A":
                # Overlap area in square micrometres: the capacitor's
                # plate-overlap area -- the geometry `c_f` above was computed
                # from (`C = A * area_cap`, see `CapacitorDevice`'s
                # docstring) -- or, for a `DeviceClassDiode` (issue #542),
                # the recognised junction's own area. Reported alongside so a
                # consumer can sanity-check the extracted value without
                # re-deriving it from the layout. `DeviceClassCapacitor`'s and
                # `DeviceClassDiode`'s area/perimeter parameters are both
                # named `A`/`P` -- distinct from
                # `DeviceClassBJT3Transistor`'s `AE`/`AB`/`AC`/`PE`/`PB`/`PC`
                # (see "Bipolar (BJT) device recognition"), so this branch
                # never fires for a bipolar device.
                params["area_um2"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "P":
                # Overlap perimeter in micrometres (issue #512): the
                # capacitor's plate-overlap perimeter, or a diode junction's
                # own perimeter (#542). KLayout's `DeviceClassCapacitor`
                # already computed this alongside `A`, but until #512 it was
                # never read back. Reported for the same sanity-check reason
                # as `area_um2`, and consumed by
                # `_apply_device_parameter_corrections` to correct `C` for a
                # deck that sets a nonzero `perim_cap_f_um`.
                params["perimeter_um"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "R":
                # Drawn-resistor device classes only (#222): KLayout's
                # `R = L / W * sheet_rho`, in ohms. MOS classes have no `R`
                # parameter, so this branch never fires for them. Already
                # carries the deck's fixed head/end-effect
                # `fixed_offset_ohm` term when the deck opted in (issue
                # #518), applied at full precision to the device itself by
                # `_apply_device_parameter_corrections`.
                params["r_ohm"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_OHM
                )
            elif param.name == "AS":
                # MOS source-diffusion junction area, square micrometres
                # (issue #695). Present regardless of `--pdk`: this is the
                # same value the unbound `M`-card form's `AS=...` already
                # carries and, since #695, the same value a `--pdk`-bound `X`
                # card's own `AS=...` carries -- so a caller reading this
                # field never needs an unbound extraction just to recover it.
                params["as_um2"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "AD":
                # MOS drain-diffusion junction area, square micrometres --
                # see `AS` above (issue #695).
                params["ad_um2"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "PS":
                # MOS source-diffusion junction perimeter, micrometres --
                # see `AS` above (issue #695).
                params["ps_um"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "PD":
                # MOS drain-diffusion junction perimeter, micrometres -- see
                # `AS` above (issue #695).
                params["pd_um"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )

        devices.append(
            {
                "name": device.expanded_name(),
                "class": class_name,
                "nets": nets,
                "params": params,
            }
        )
        device_counts[class_name] = device_counts.get(class_name, 0) + 1

    devices.sort(key=lambda entry: entry["name"])
    return devices, device_counts


def _describe_matched_device_groups(
    matched_device_groups: Mapping[str, Sequence[str]] | None,
    devices: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build the response's ``matched_device_groups[]`` array and its
    aggregate ``warnings[]`` entries (issue #1018).

    ``matched_device_groups`` is ``{<group name>: (<instance name>, ...),
    ...}`` -- ``run_extract``'s own parsed form of ``--matched-group
    NAME=INST1,INST2[,...]``. ``devices`` is the already-built ``devices[]``
    array (see :func:`_describe_devices`), so every comparison reads the
    exact same rounded ``params`` values every other consumer of the response
    sees -- this is a report on the extracted netlist, not a second,
    independent measurement.

    For each declared group, every member name is resolved against
    ``devices[].name`` (a KLayout-synthesized ``"$<n>"`` instance name unless
    the deck's writer names it otherwise); a name matching no extracted
    device is collected into that group's ``unresolved_instances`` rather
    than raising -- a caller may legitimately reuse one group declaration
    across several layout variants, the same tolerant convention
    ``--critical-net`` already follows for an unmatched net name. With two or
    more *resolved* members, every parameter name present in **every**
    resolved member's ``params`` (the intersection, so a group mixing device
    classes only compares the fields they actually share) is compared for
    exact equality across the group -- already-rounded values
    (``_PARAM_PRECISION_UM``/``_PARAM_PRECISION_OHM``), so no separate
    numeric-tolerance concept is needed. Fewer than two resolved members
    (nothing to compare) always reports an empty ``mismatched_fields``.

    Returns ``(matched_device_groups_report, warnings)``: the former is one
    entry per declared group, in the order given, each ``{"name": <group
    name>, "instances": [<instance name>, ...], "unresolved_instances":
    [<instance name>, ...], "mismatched_fields": [{"field": <param name>,
    "values": {<instance name>: <float>, ...}}, ...]}`` (``instances`` echoes
    the declaration verbatim, ``unresolved_instances`` sorted); the latter is
    one aggregate prose ``warnings[]`` entry per group with unresolved
    members and one per group with mismatched fields (both empty, `[]`
    overall, when ``matched_device_groups`` is empty/``None``).
    """
    if not matched_device_groups:
        return [], []

    devices_by_name = {device["name"]: device for device in devices}
    groups: list[dict[str, Any]] = []
    warnings: list[str] = []

    for group_name, instance_names in matched_device_groups.items():
        resolved: list[dict[str, Any]] = []
        unresolved: list[str] = []
        for instance_name in instance_names:
            device = devices_by_name.get(instance_name)
            if device is None:
                unresolved.append(instance_name)
            else:
                resolved.append(device)

        mismatched_fields: list[dict[str, Any]] = []
        if len(resolved) >= 2:
            common_fields = set(resolved[0]["params"])
            for device in resolved[1:]:
                common_fields &= set(device["params"])
            for field in sorted(common_fields):
                values = {
                    device["name"]: device["params"][field] for device in resolved
                }
                if len(set(values.values())) > 1:
                    mismatched_fields.append({"field": field, "values": values})

        groups.append(
            {
                "name": group_name,
                "instances": list(instance_names),
                "unresolved_instances": sorted(unresolved),
                "mismatched_fields": mismatched_fields,
            }
        )

        if unresolved:
            names_str = ", ".join(repr(name) for name in sorted(unresolved))
            warnings.append(
                f"matched-group {group_name!r} names instance(s) {names_str} "
                "that match no extracted device in this layout -- geometry "
                "consistency was not checked for them. See "
                "docs/cli/extract.md's 'Matched-device geometry check' "
                "section."
            )
        if mismatched_fields:
            fields_str = "; ".join(
                f"{entry['field']}: "
                + ", ".join(
                    f"{name}={value}" for name, value in entry["values"].items()
                )
                for entry in mismatched_fields
            )
            instances_str = ", ".join(instance_names)
            warnings.append(
                f"matched-group {group_name!r} declares {instances_str} as "
                "intentionally matched, but their extracted geometry "
                f"diverges -- {fields_str} -- this likely breaks the "
                "layout's matching assumption (a hand-edit slip or a "
                "mis-parameterized generator call). See "
                "docs/cli/extract.md's 'Matched-device geometry check' "
                "section."
            )

    return groups, warnings


def _describe_nets(circuit: kdb.Circuit) -> list[dict[str, Any]]:
    """Build the response's ``nets[]`` array."""
    pin_nets = {pin.expanded_name() for pin in _each_pin_net(circuit)}

    nets: list[dict[str, Any]] = []
    for net in circuit.each_net():
        raw_name = net.expanded_name()
        nets.append(
            {
                "name": spice_safe_net_name(raw_name),
                "pin": raw_name in pin_nets,
                "device_count": net.terminal_count(),
            }
        )

    nets.sort(key=lambda entry: entry["name"])
    return nets


def _detect_merged_net_labels(nets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the response's ``merged_net_labels[]`` array (issue #470).

    KLayout's ``Net.expanded_name()`` joins every distinct label found on one
    electrical net with ``,`` (e.g. two labels ``Y`` and ``OUT`` shorted
    together on layout come back as the single net name ``"Y,OUT"``,
    verified for issue #312's SPICE-instance-name-sanitization fix). That
    join is otherwise silent: `nets[]`' ``name`` field carries it, but
    nothing calls it out as the layout asserting a connectivity equality the
    caller may not have intended.

    Scans ``nets`` (the already-built ``nets[]`` array, so this reuses the
    same name this module reports elsewhere rather than re-querying the
    circuit) for any net whose name splits into 2+ parts on ``|`` -- the
    netlist-consistent spelling :func:`spice_safe_net_name` already rewrote
    ``nets[].name`` to (issue #696), matching the written SPICE netlist's own
    ``.SUBCKT``/instance-line node references. Returns one entry per match:
    ``{"net": "<full joined name>", "labels": ["Y", "OUT", ...]}`` -- ``net``
    is therefore a usable key into the written netlist, not a separately
    (comma-) spelled alias of it. Always a list; empty when no net carries
    multiple labels.

    Known limitation (heuristic, not exact): a label that legitimately
    contains a literal ``|`` is indistinguishable from a real multi-label
    collision by this substring split -- see docs/cli/extract.md's "Merged
    net labels" section.
    """
    merged: list[dict[str, Any]] = []
    for net in nets:
        name = net["name"]
        labels = name.split("|")
        if len(labels) < 2:
            continue
        merged.append({"net": name, "labels": labels})
    return merged


#: Every anonymous KLayout-synthesized net name reported anywhere in this
#: module's JSON (``devices[].nets[...]``, ``nets[].name``, etc.) has already
#: passed through :func:`spice_safe_net_name`, which backslash-escapes a
#: raw ``Net.expanded_name()`` leading ``$`` to match the written netlist's
#: own node spelling (issue #1162) -- so the *reported* prefix is ``\$``,
#: not KLayout's own raw ``$``.
_ANONYMOUS_NET_PREFIX = "\\$"


def _detect_unbiased_pmos_body_nets(
    devices: list[dict[str, Any]], deck: ExtractionDeck
) -> list[dict[str, Any]]:
    """Build the response's ``unbiased_pmos_body_nets[]`` array (issue #555).

    Scans the already-built ``devices[]`` array (so this reuses the exact
    terminal-net names ``_describe_devices`` already read off the netlist)
    for every PMOS device (``device["class"] == deck.pfet_class``) whose body
    terminal (``nets["b"]``) is an anonymous, KLayout-synthesized net --
    identified by ``Net.expanded_name()``'s own ``"$<n>"`` placeholder
    convention for a net with no drawn label, reported here already
    backslash-escaped to ``"\\$<n>"`` by :func:`spice_safe_net_name` (issue
    #1162, matching the written netlist's own node spelling -- the same
    convention ``tests/test_extract.py`` already asserts against, e.g.
    ``pfet["nets"]["b"].startswith("\\$")``). A *named* net -- including the
    deck's own synthesized global substrate net (e.g. ``"vsubs"``) -- never
    matches, so an NMOS body (tied via ``connect_global``) or a well-labelled
    PMOS body (a deck with a real ``well_label``/``tap`` layer, e.g. sky130)
    never appears here.

    Returns one entry per affected device, ``{"device": <device instance
    name>, "net": <anonymous net name>}``, sorted by device name for
    deterministic output (matching ``devices[]``/``nets[]``'s own sort
    discipline). Always a list; empty when no PMOS device's body net is
    anonymous.
    """
    unbiased: list[dict[str, Any]] = []
    for device in devices:
        if device["class"] != deck.pfet_class:
            continue
        body_net = device["nets"].get("b")
        if body_net is not None and body_net.startswith(_ANONYMOUS_NET_PREFIX):
            unbiased.append({"device": device["name"], "net": body_net})
    unbiased.sort(key=lambda entry: entry["device"])
    return unbiased


#: Terminal-name labels for a MOS-like device (identified below by the
#: presence of a ``"g"`` terminal key -- the one terminal name no other
#: recognised device class in this repo uses; see
#: :func:`_detect_single_terminal_nets`). Keyed by the same lower-cased
#: terminal name :func:`_describe_devices` already writes into
#: ``devices[].nets``.
_MOS_TERMINAL_KIND_LABELS = {"g": "gate", "s": "source", "d": "drain", "b": "body"}


def _detect_single_terminal_nets(
    devices: list[dict[str, Any]], nets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build the response's ``single_terminal_nets[]`` array (issue #596).

    Scans the already-built ``nets`` array (``_describe_nets``'s output,
    each carrying ``device_count`` -- literally ``Net.terminal_count()`` --
    and ``pin``) for every net with ``device_count == 1`` and ``pin: False``:
    a net that touches exactly one device terminal and is not a declared
    top-level pin. There is no DC path through such a node from anywhere
    else in the netlist -- ngspice reports ``singular matrix: check node
    <net>`` on it, several stages downstream of where the defect is
    structurally detectable. A gate terminal is the strongest form of this
    defect (a floating MOS input, essentially never intentional); a
    source/drain/body/resistor-style terminal can legitimately be a
    single-terminal net (e.g. an intentionally unterminated dummy's
    diffusion tie), so this reports every match but lets the caller (and
    the matching ``warnings[]`` prose below) weight ``terminal_kind ==
    "gate"`` more heavily than the rest.

    Cross-references the already-built ``devices`` array (mirroring
    :func:`_detect_unbiased_pmos_body_nets`'s reuse of the same terminal-net
    names ``_describe_devices`` already read off the netlist) to find the
    exact device/terminal that owns each flagged net -- a net's
    ``device_count`` disagreeing with the number of matching
    ``devices[].nets`` entries would mean the two already-built arrays
    disagree with each other, which should not happen; that net is skipped
    rather than guessed at.

    ``terminal_kind`` is ``"gate"``/``"source"``/``"drain"``/``"body"`` for a
    MOS-like device (identified by the presence of a ``"g"`` terminal on
    that device -- the one terminal name no other recognised device class in
    this repo's decks uses: drawn resistors/capacitors use ``"a"``/``"b"``
    (plus ``"w"`` for a bulk terminal), diodes use ``"a"``/``"c"``, bipolar
    devices use ``"c"``/``"b"``/``"e"``), else the literal terminal name
    (e.g. ``"a"``, ``"w"``) -- the "resistor-equivalent" case the issue
    calls out, reported as-is rather than guessed at with a device-specific
    label.

    Returns one entry per affected net, ``{"net": <net name>, "device": <owning
    device instance name>, "terminal": <lower-cased terminal key>,
    "terminal_kind": <str>}``, sorted by net name for deterministic output
    (matching ``nets[]``'s own sort discipline). Always a list; empty when
    every net either has zero or 2+ device terminals, or is a declared pin.
    """
    # net name -> (owning device name, terminal key) for every connected
    # device terminal, built once so a flagged net (device_count == 1) can be
    # traced back to the exact device/terminal without re-scanning `devices`
    # per net.
    owners: dict[str, list[tuple[str, str]]] = {}
    device_terminal_keys: dict[str, set[str]] = {}
    for device in devices:
        device_terminal_keys[device["name"]] = set(device["nets"].keys())
        for terminal_key, net_name in device["nets"].items():
            if net_name is None:
                continue
            owners.setdefault(net_name, []).append((device["name"], terminal_key))

    single_terminal: list[dict[str, Any]] = []
    for net in nets:
        if net["pin"] or net["device_count"] != 1:
            continue
        matches = owners.get(net["name"], [])
        if len(matches) != 1:
            continue
        device_name, terminal_key = matches[0]
        terminal_keys = device_terminal_keys.get(device_name, set())
        if "g" in terminal_keys:
            terminal_kind = _MOS_TERMINAL_KIND_LABELS.get(terminal_key, terminal_key)
        else:
            terminal_kind = terminal_key
        single_terminal.append(
            {
                "net": net["name"],
                "device": device_name,
                "terminal": terminal_key,
                "terminal_kind": terminal_kind,
            }
        )
    single_terminal.sort(key=lambda entry: entry["net"])
    return single_terminal


def _describe_layers_in_set(
    path: str, layer_set: frozenset[tuple[int, int]], *, invert: bool = False
) -> list[dict[str, Any]]:
    """Shared helper behind ``ignored_layers``/``device_recognition_only_
    layers`` (issue #619): enumerate the input stream's layers (reusing
    ``layers.py``'s existing per-layer walk, the same one ``klt drc``'s
    coverage report leans on) and return the shape-bearing ``(layer,
    datatype)`` pairs that are in ``layer_set`` (``invert=False``) or *not* in
    it (``invert=True``). Each entry carries its stream ``shapes`` count.
    Empty-layer entries (``shapes == 0``) are skipped; the list is sorted by
    ``(layer, datatype)``.
    """
    from .layers import layers_report

    described: list[dict[str, Any]] = []
    for entry in layers_report(path)["layers"]:
        if entry["shapes"] <= 0:
            continue
        in_set = (entry["layer"], entry["datatype"]) in layer_set
        if in_set == invert:
            continue
        described.append(
            {
                "layer": entry["layer"],
                "datatype": entry["datatype"],
                "shapes": entry["shapes"],
            }
        )
    described.sort(key=lambda e: (e["layer"], e["datatype"]))
    return described


def _describe_ignored_layers(path: str, deck: ExtractionDeck) -> list[dict[str, Any]]:
    """Build the response's ``ignored_layers[]`` array (issue #220).

    Returns the shape-bearing ``(layer, datatype)`` pairs that are *not* in
    ``deck.connectivity_layers`` -- geometry the extraction connectivity graph
    never reads. Each entry carries its stream ``shapes`` count so a consumer
    can judge whether the amount is material (a stray annotation vs. a whole
    block routed on an undeclared metal level).

    Note this does *not* catch a layer that is read for device recognition
    only, never as a ``metals``/``vias`` connectivity level -- see
    :func:`_describe_device_recognition_only_layers` for that distinct case
    (issue #619).
    """
    return _describe_layers_in_set(path, deck.connectivity_layers, invert=True)


def _describe_device_recognition_only_layers(
    path: str, deck: ExtractionDeck
) -> list[dict[str, Any]]:
    """Build the response's ``device_recognition_only_layers[]`` array
    (issue #619).

    Returns the shape-bearing ``(layer, datatype)`` pairs in
    ``deck.device_recognition_only_layers`` -- layers the deck *reads* (for a
    ``bipolars``/``capacitors``/``resistors``/``diodes`` device-recognition
    role) but never treats as a ``metals``/``vias`` connectivity level, so
    shapes there are invisible to net-merging even though they are not
    "ignored" in the ``ignored_layers`` sense. Each entry carries its stream
    ``shapes`` count. This is the "read but not merged" counterpart to
    ``ignored_layers``'s "never read at all" -- see
    ``ExtractionDeck.device_recognition_only_layers``'s docstring for why the
    distinction matters (sky130's own met3/met4 hid a routing-connectivity
    gap behind this exact ambiguity before its ``metals`` stack grew to
    cover them too).
    """
    return _describe_layers_in_set(
        path, deck.device_recognition_only_layers, invert=False
    )


def _describe_parasitics_metal_gaps(
    deck: ExtractionDeck, parasitics_deck: ParasiticsDeck
) -> list[dict[str, Any]]:
    """Build the ``parasitics.metals_without_coefficient[]`` array (issue #547).

    ``_compute_parasitics`` walks ``parasitics_deck.metals`` index-aligned
    against ``deck.metals`` (the extraction deck's declared metal stack,
    index 0 = the bottom level) and silently contributes zero R/C for any
    stack level that has no coefficient -- either because
    ``parasitics_deck.metals`` is shorter than ``deck.metals`` (truncation)
    or the entry at that index is explicitly ``None``. Both are the same gap
    from a caller's perspective: the level's R and C are missing from every
    net's reported parasitics, with nothing else in the JSON to say so.

    Returns one entry per gap, ``{"metal_index": int, "layer": int,
    "datatype": int}`` (``metal_index`` is 0-based, matching ``deck.metals``
    and ``parasitics_deck.metals``' shared indexing -- index 0 is the deck's
    bottom-most metal level, e.g. gf180mcu's Metal1), sorted by
    ``metal_index``. Empty when every declared metal level has a coefficient
    -- the common case, and always true when ``--parasitics`` was not
    requested (callers only invoke this when ``parasitics_deck is not
    None``).
    """
    gaps: list[dict[str, Any]] = []
    for i, layer in enumerate(deck.metals):
        if i >= len(parasitics_deck.metals) or parasitics_deck.metals[i] is None:
            gaps.append({"metal_index": i, "layer": layer[0], "datatype": layer[1]})
    return gaps


def _describe_parasitics_overlap_gaps(
    deck: ExtractionDeck, parasitics_deck: ParasiticsDeck
) -> list[dict[str, Any]]:
    """Build the ``parasitics.overlap_pairs_without_coefficient[]`` array
    (issue #760) -- the ``metals_without_coefficient``-style (#547) gap
    report for the vertical-overlap coupling coefficient family.

    ``_compute_parasitics`` walks ``parasitics_deck.metal_overlaps``
    index-aligned against **adjacent pairs** of ``deck.metals`` (pair ``i``
    is between metal levels ``i`` and ``i+1``) and silently contributes zero
    coupling capacitance for any adjacent pair that has no coefficient --
    either because ``metal_overlaps`` is shorter than ``len(deck.metals) -
    1`` (truncation, including the empty-tuple default every deck starts
    from) or the entry at that pair index is explicitly ``None``. Both are
    the same gap from a caller's perspective: that pair's area still charges
    to ground in full, exactly as if this feature did not exist, with
    nothing else in the JSON to say so -- a silent zero, not a silent
    approximation.

    Returns one entry per gap, ``{"lower_metal_index": int, "upper_metal_index":
    int, "lower_layer": int, "lower_datatype": int, "upper_layer": int,
    "upper_datatype": int}`` (0-based, matching ``deck.metals``' indexing --
    pair index 0 is between the deck's bottom two metal levels), sorted by
    ``lower_metal_index``. Empty when every declared adjacent metal-level
    pair has a coupling coefficient -- true for both shipped decks today
    (each curates one coefficient per adjacent pair its ``metals`` stack
    declares), and always true when ``--parasitics`` was not requested
    (callers only invoke this when ``parasitics_deck is not None``). A deck
    with fewer than two metal levels declared has no adjacent pair at all,
    so this is always empty for it, matching ``deck.metals`` having no
    consecutive-index pair to check.
    """
    gaps: list[dict[str, Any]] = []
    for i in range(len(deck.metals) - 1):
        if (
            i >= len(parasitics_deck.metal_overlaps)
            or parasitics_deck.metal_overlaps[i] is None
        ):
            lower_layer = deck.metals[i]
            upper_layer = deck.metals[i + 1]
            gaps.append(
                {
                    "lower_metal_index": i,
                    "upper_metal_index": i + 1,
                    "lower_layer": lower_layer[0],
                    "lower_datatype": lower_layer[1],
                    "upper_layer": upper_layer[0],
                    "upper_datatype": upper_layer[1],
                }
            )
    return gaps


def _each_pin_net(circuit: kdb.Circuit) -> list[kdb.Net]:
    """The distinct nets exposed as circuit pins."""
    result = []
    for pin in circuit.each_pin():
        net = circuit.net_for_pin(pin.id())
        if net is not None:
            result.append(net)
    return result
