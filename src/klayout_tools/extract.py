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

import os
import re
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
from ._paths import _load_request_json
from ._paths import load_request_arg as _shared_load_request_arg
from ._paths import validate_request_shape as _shared_validate_request_shape
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
# because `_DEF_NET_NAME_PROPERTY_ID` is used directly by `_extract_netlist`'s
# `--def-net-names` diagnostic, `_sanitize_instance_name` is used directly by
# the RC-parasitics subsystem (`extract_parasitics.py`, issue #1572, which
# imports it from `extract_abstract.py` itself rather than through this
# re-export) -- and the test suite imports `_sanitize_instance_name` from
# `klayout_tools.extract` by name.
from .extract_abstract import _DEF_NET_NAME_PROPERTY_ID as _DEF_NET_NAME_PROPERTY_ID
from .extract_abstract import (
    _abstract_cell_body_identity_cover,
    _abstract_cell_global_net_ports,
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

# The RC-parasitics subsystem lives in its own module (issue #1572, following
# the same split as `extract_abstract.py`/`extract_spef.py` above). This
# module's own remaining call sites are exactly two -- `run_extract` calls
# `_inject_parasitics` once, `_extract_netlist` calls `_compute_parasitics`
# and `_detect_dead_metal` once each -- but `spice_safe_net_name` is
# re-exported (`X as X`) because it has external importers of its own
# (`lvs.py`, `netlist_digest.py`) as well as many call sites in this module.
from .extract_parasitics import (
    SPICE_HIERARCHY_SEPARATOR,
    SPICE_SAFE_HIERARCHY_JOIN,
    _compute_parasitics,
    _detect_dead_metal,
    _inject_parasitics,
)
from .extract_parasitics import spice_safe_net_name as spice_safe_net_name

# The netlist-report-description subsystem lives in its own module (issue
# #2070, the fourth split of this one after `extract_abstract.py`,
# `extract_parasitics.py` and `extract_spef.py`). It shapes an already-resolved
# `kdb.Circuit`/`kdb.LayoutToNetlist` into the response's `devices[]`/`nets[]`
# arrays and layer/net warning lists -- the report layer, not the recognition
# engine. Every name below has a call site in this module (`run_extract` for
# all but `_net_label_positions`, which `_extract_netlist` calls while the
# layout is still open), so none needs an `X as X` re-export alias; the test
# suite's `from klayout_tools.extract import _describe_devices`-style imports
# keep resolving through them unchanged. `extract_report.py`'s own three
# internal helpers (`_describe_layers_in_set`, `_pin_index_by_net_id`,
# `_each_pin_net`) have no caller here and are deliberately not imported.
from .extract_report import (
    _describe_device_recognition_only_layers,
    _describe_devices,
    _describe_ignored_layers,
    _describe_matched_device_groups,
    _describe_nets,
    _describe_parasitics_metal_gaps,
    _describe_parasitics_overlap_gaps,
    _detect_merged_net_labels,
    _detect_single_terminal_nets,
    _detect_unbiased_pmos_body_nets,
    _net_label_positions,
)

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
from .metrics import is_registered
from .pdk import PdkNotFoundError, find_pdk
from .pdk_models import (
    MOS_FLAVOUR_PROPERTY,
    DeviceBinding,
    ModelBindingError,
    create_model_binding_delegate,
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

# `run_extract()`'s own field name -> its declared METRICS2.1-style name in
# `metrics.py`'s registry (issue #1848, adopting the #247 registry beyond its
# `layout-metrics`/`klt drc` (#1847) adopters). Additive: the `metrics` block
# these back is a *parallel* object alongside the existing fields, never a
# replacement for them -- see `docs/design/metric-namespace.md` for the
# additive-vs-rename decision. `_METRIC_NAME_BY_FIELD` covers the three
# fields always present; `_PARASITICS_METRIC_NAME_BY_FIELD` covers the eight
# `--parasitics`-only fixed numeric fields (`parasitics_report`'s own keys,
# not `run_extract()`'s top-level ones). `device_counts` (the per-device-class
# dict) is deliberately excluded from both -- see `run_extract`'s docstring
# "metrics" paragraph for why.
_METRIC_NAME_BY_FIELD = {
    "device_count": "extract__device__count",
    "net_count": "extract__net__count",
    "pin_count": "extract__pin__count",
}
_PARASITICS_METRIC_NAME_BY_FIELD = {
    "r_count": "extract__resistor__count",
    "c_count": "extract__capacitor__count",
    "cc_count": "extract__coupling__capacitor__count",
    "l_count": "extract__inductor__count",
    "total_resistance_ohm": "extract__resistance__ohm",
    "total_capacitance_ff": "extract__capacitance__ff",
    "total_coupling_capacitance_ff": "extract__coupling__capacitance__ff",
    "total_inductance_nh": "extract__inductance__nh",
}

assert all(is_registered(name) for name in _METRIC_NAME_BY_FIELD.values())
assert all(is_registered(name) for name in _PARASITICS_METRIC_NAME_BY_FIELD.values())

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


#: The one top-level field a ``klt extract`` request document must carry
#: (issue #1867). ``deck`` is deliberately *not* required here: an omitted
#: deck is already reported as an application error (exit 1, "argument --deck
#: is required") by ``cli/extract_cmd.py``'s own ``run`` rather than as a
#: request-shape error -- see docs/cli/extract.md's exit-code contract.
_REQUIRED_REQUEST_FIELDS = ("file",)

#: Contract identifier a ``klt extract`` request document may declare in its
#: optional ``schema`` field (issue #1867).
REQUEST_SCHEMA = "klt.extract.request/1"


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt extract`` request JSON file.

    Raises :class:`ExtractError` if the file is missing/unreadable, not valid
    JSON, or missing the required top-level ``file`` field. Does not require
    a ``schema`` field, matching ``klt lvs``/``klt sim``'s ``load_request``
    (user-authored input, never emitted by this tool) -- when one *is*
    present, ``cli/extract_cmd.py`` checks it against :data:`REQUEST_SCHEMA`.
    """
    request = _load_request_json(request_path, ExtractError)
    return _validate_request_shape(request, "request file")


def _validate_request_shape(data: Any, source: str) -> dict[str, Any]:
    """Shared ``file`` shape check for a JSON-decoded ``klt extract``
    request, however it was sourced (file, inline JSON, stdin). ``source`` is
    folded into the "must be a JSON object" error for context.
    """
    return _shared_validate_request_shape(
        data,
        source,
        error_cls=ExtractError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
    )


def load_request_arg(value: str) -> tuple[dict[str, Any], str]:
    """Resolve a ``klt extract`` request-document argument (issue #1867) into
    a request dict plus the directory relative paths inside it resolve
    against.

    ``value`` is one of the same three forms every other request-taking
    ``klt`` verb accepts (see
    :func:`klayout_tools._paths.load_request_arg` and
    docs/cli/extract.md): ``"-"`` for stdin, a path to an existing request
    JSON file, or an inline JSON object string. Relative paths inside the
    document resolve against the document's own directory for the file form,
    and against the current working directory for the stdin/inline forms.

    Raises :class:`ExtractError` for any read/parse/shape failure -- the same
    exception type :func:`load_request` raises, so callers do not need to
    distinguish the three forms.
    """
    return _shared_load_request_arg(
        value,
        error_cls=ExtractError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
        load_request_fn=load_request,
    )


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
    parasitics_nets: Sequence[str] | None = None,
    parasitics_top_cell_only: bool = False,
    distributed_rc: bool = False,
    def_net_connections: Mapping[str, Sequence[tuple[str, str]]] | None = None,
    mom_rlc_net: str | None = None,
    mom_rlc_resistance_ohm: float | None = None,
    mom_rlc_capacitance_ff: float | None = None,
    mom_rlc_inductance_nh: float | None = None,
    matched_device_groups: Mapping[str, Sequence[str]] | None = None,
    def_pins: frozenset[str] | None = None,
    pin_source_cells: frozenset[str] | None = None,
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
    of net names), every promoted pin whose net is *not* named by the set is
    demoted back to an internal net -- it keeps its name, it is simply not
    exposed as a top-level pin. This is the fix for labelling an internal
    node of a lumped schematic device (e.g. one tap of a metal-option
    ladder modelled as a single series device) purely for documentation --
    today that label always promotes the node to a pin, which blocks ``klt
    lvs``'s ``options.combine_devices`` from folding the series chain the
    reference netlist models as one device. A promoted net's name matches
    the declared set if *any* of its comma-joined component labels is in
    ``declared_pins`` (issue #1687) -- not a whole-string match -- since
    KLayout joins every distinct text label found on one electrical net into
    a single, comma-separated ``Net.name`` (see ``spice_safe_net_name``'s
    docstring), so a net composed from two independently-labelled blocks
    (e.g. a library cell's own pin label plus a top-level wire's label
    landing on the same pad) can carry a joined name no single declared
    string can ever equal. Reuses :func:`_reconcile_top_pins` exactly as
    ``top_cell_pins_only`` does, with a different demote-set: every
    currently-promoted pin name whose component-label set does not
    intersect ``declared_pins``. A ``warnings`` entry lists any net demoted
    this way, and a separate entry lists any declared name that matched no
    promoted net's label set (a likely typo, not silently ignored). A third
    entry (issue #2000) lists any declared name that matches 2+ physically
    disconnected nets that happen to carry the identical drawn label -- both
    stay promoted (demoting either would risk hiding a genuine split-net
    connectivity defect from a downstream ``klt lvs`` reference netlist), so
    ``pin_count`` can exceed ``len(declared_pins)`` in that case; see
    :func:`_duplicated_declared_pin_names`'s own docstring. ``None``
    (the default) skips this reconciliation entirely -- byte-identical to
    today's behavior, same invariant ``top_cell_pins_only``'s own default
    preserves. Applied after ``top_cell_pins_only``'s own reconciliation,
    and only ever *further* demotes -- it cannot re-promote a net
    ``top_cell_pins_only`` already kept internal.

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
    promoted net's label set. A third entry (issue #2000) mirrors
    ``declared_pins``'s own: any ``def_pins`` name matching 2+ physically
    disconnected nets that happen to share a drawn label, since both stay
    promoted and ``pin_count`` can then exceed ``len(def_pins)``. Applied
    *after* ``declared_pins``'s own
    reconciliation (when both are given), so it can only further restrict.
    ``None`` (the default) skips this reconciliation entirely --
    byte-identical to today's behavior. See ``docs/cli/extract.md``'s
    "DEF-derived declared pins" section for a worked example against the
    real `gcd` corpus fixture.

    ``pin_source_cells`` (the ``--pin-source-cells`` flag, issue #1513) is a
    third, *positional* declared-pin mechanism for the case neither
    ``declared_pins`` nor ``def_pins`` can express cleanly: composing
    several already-independently-verified blocks -- at least one of them a
    placed-and-routed macro with its own generic internal pin labels (``A``,
    ``X``, ``Q``, ``Y``, ...) -- into one flat top-level layout via `klt
    gen-compose` plus hand-drawn interconnect, with no single governing DEF
    of the *composition* itself to anchor ``def_pins`` on. ``top_cell_pins_only``
    cannot help either: the composition's own hand-drawn interconnect labels
    necessarily live in an *instanced* sub-cell (the routing cell the
    composition step created), not literally in the new top cell, so
    ``--top-cell-pins`` demotes them right alongside the genuine internal
    noise it is meant to exclude. And ``declared_pins``/``def_pins`` match by
    *string* -- a promoted net's own comma-joined name -- which cannot
    distinguish two distinct, unrelated nets that both happen to carry the
    same generic label text as one of several joined components (e.g. two
    independently-labelled macros that each happen to use ``CLK``
    internally): declaring that string promotes *both*, not just the
    intended one.

    ``pin_source_cells`` is a set of cell names (the ``--pin-source-cells``
    flag's comma-separated argument): every drawn pin-name label found
    anywhere under the top cell whose *immediate owning cell* has one of
    these names is resolved to its actual extracted net by probing that
    label's own composed-frame position (:func:`_pin_source_cell_net_names`)
    -- not by matching its string against anything. This sidesteps both
    failure modes at once: a label drawn in an instanced sub-cell is found
    (unlike ``--top-cell-pins``, which excludes it by cell depth alone), and
    two coincidentally-same-spelled labels in *different* cells resolve to
    two different, independently-tracked nets (unlike ``def_pins``'s
    component-string match, which cannot tell them apart at all). Every
    currently-promoted pin whose net was not reached this way is demoted,
    exactly as ``declared_pins``/``def_pins`` demote on a miss; a
    ``warnings`` entry lists any net demoted this way, and a separate entry
    lists any label found in a named cell that resolved to no drawn
    conductor at its own position. Applied *after* ``declared_pins``'s and
    ``def_pins``'s own reconciliations (when given), so it can only further
    restrict -- it never re-promotes a net either of those already kept
    internal. ``None`` (the default) skips this reconciliation entirely --
    byte-identical to today's behavior. See ``docs/cli/extract.md``'s
    "Pin-source cells" section for the full mechanism and a worked example.

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

    ``parasitics_nets`` (``klt extract --parasitics-net <net>``, repeatable,
    issue #1700) scopes the *ground* R/C pass itself -- the base per-net
    ``(R, C)`` measurement every other parasitics flag layers onto -- down to
    the named nets, instead of measuring every net in the design. This is the
    cost half of issue #1699: the ground pass calls
    ``LayoutToNetlist.polygons_of_net`` once per net per conductor role, so
    on a large mixed-signal top cell it dominates runtime even when the
    caller only wants R/C for a handful of nets. Membership reuses
    ``critical_nets``' own ``frozenset[str]`` pattern verbatim (the escaped
    spelling ``parasitics.nets[].net`` reports, issue #1162), deliberately
    rather than introducing a second net-classification mechanism. A net not
    named here is measured not at all: it gets no ``parasitics.nets[]``
    entry, no injected ``R``/``C`` cards, and -- since coupling is computed
    from the geometry this pass caches -- can neither give nor receive
    coupling capacitance, so only a pair of *both*-named nets can couple.
    Requires ``parasitics=True``, same convention as ``critical_nets`` above.
    A name matching no net with ground-eligible parasitics geometry is a
    ``warnings`` entry, not an :class:`ExtractError` -- again matching
    ``critical_nets`` rather than ``mom_net``'s stricter contract.
    ``None``/empty (the default) runs the full-layout pass -- byte-identical
    to before this feature existed.

    ``parasitics_top_cell_only`` (``klt extract --parasitics-top-cell-only``,
    issue #1704) additionally splits each net's ground R/C into the portion
    drawn directly in the top cell versus the portion drawn inside an
    instantiated sub-block -- the additive ``resistance_ohm_top_cell``/
    ``capacitance_ff_top_cell`` fields on each ``parasitics.nets[]`` entry
    (``None`` unless this flag is given). Lets a caller who has already
    extracted a sub-block separately subtract its contribution back out of a
    composed top-level net's R/C, rather than only having one undifferentiated
    scalar. Ground terms only (Pass 1) -- coupling capacitance attribution is
    out of scope for this increment. See :func:`_compute_parasitics`'s
    docstring for the algorithm (built once per curated layer, not per net or
    instance) and its documented "exact in area, not exactly additive in
    perimeter" caveat for a net whose conductor spans an instance boundary.
    Requires ``parasitics=True``, same convention as ``critical_nets``/
    ``parasitics_nets`` above. ``False`` (the default) computes neither field
    and runs no extra ``Region`` work at all -- byte-identical to before this
    feature existed.

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
            "metrics": {
                "extract__device__count": <int>,
                "extract__net__count": <int>,
                "extract__pin__count": <int>,
                # The eight entries below are present only when
                # `--parasitics` was given -- see the "metrics" paragraph
                # below.
                "extract__resistor__count": <int>,
                "extract__capacitor__count": <int>,
                "extract__coupling__capacitor__count": <int>,
                "extract__inductor__count": <int>,
                "extract__resistance__ohm": <float>,
                "extract__capacitance__ff": <float>,
                "extract__coupling__capacitance__ff": <float>,
                "extract__inductance__nh": <float>,
            },
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
                    # Issue #1666: the GDS-level instance placement chain
                    # (outermost first) this device's recognition shape
                    # positionally resolved to -- `[]` when it resolved to
                    # none (drawn directly in the top cell, the common case
                    # for a design with no repeated sub-cells).
                    "instance_path": [
                        {"cell": str, "array_index": [int, int] | None}, ...
                    ],
                },
                ...
            ],
            "nets": [
                {
                    "name": str, "pin": bool, "device_count": int,
                    # Issue #1540: unique across every net *object* in this
                    # circuit, unlike "name" (several distinct nets can
                    # collide on one name -- see the "nets[]" schema note
                    # below).
                    "net_id": int,
                    # 0-based `.SUBCKT` port position (`None` if not a
                    # promoted pin) -- issue #1540.
                    "pin_index": int | None,
                    # Drawn-label geometry naming this net -- issue #1540.
                    "label_positions_um": [
                        {"text": str, "x_um": float, "y_um": float}, ...
                    ],
                },
                ...
            ],
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

    ``nets[].net_id``/``pin_index``/``label_positions_um`` (issue #1540)
    disclose, per net, what ``name`` alone cannot when a flat extraction of a
    layout with internally-repeated sub-cells collides two or more genuinely
    distinct nets onto one ``name`` string (e.g. a ring of identical 2-input
    stages, each stage's output touching the next stage's input -- every
    junction node, plus the block's one true external pin, reads as the same
    joined ``a|y``-style name; KLayout's own ``NetlistSpiceWriter``
    disambiguates them at *write* time with a ``$1``/``$2``-style suffix this
    module does not control, see :func:`spice_safe_net_name`'s docstring).
    ``net_id`` is ``net.cluster_id`` -- unique across every net *object* in
    this circuit, the same convention ``parasitics.nets[].net_id`` already
    uses for the identical "several distinct nets share one label" shape
    (issue #765/#811). ``pin_index`` is that net's 0-based position in the
    written ``.SUBCKT``/instance-line port order (``circuit.each_pin()``,
    the exact sequence ``NetlistSpiceWriter`` iterates) when it is a promoted
    pin, ``None`` otherwise -- resolving a specific ``.SUBCKT`` port index
    straight back to a ``nets[]`` entry, positionally, with no separate
    ``klt lvs`` run against a reference schematic needed.
    ``label_positions_um`` is ``[{"text": str, "x_um": float, "y_um": float},
    ...]`` -- every drawn text-label shape naming this net, in this circuit's
    own local coordinate frame (same convention
    ``black_box_regions[].bbox_um``/``unmodelled_poly[].bbox_um`` already
    use) -- the mechanism a caller with independent floorplan knowledge (the
    physical location it externally routed a wire to, e.g. from `klt
    place-and-route`'s DEF or its own generator's placement record) uses to
    positively identify *which* of several identically-named collided
    entries is the one it means, without guessing from the name alone.
    ``--pins``/``--top-cell-pins``/``--def-pins``/``--pin-source-cells`` all
    still require the caller to already know the true interface (a name, a
    DEF ``PINS`` list, or a cell-depth heuristic) -- none of them help
    *discover* it when the names themselves are ambiguous, which is exactly
    what these three fields are for. See ``docs/cli/extract.md``'s "Net-name
    collisions from internally-repeated sub-cells" section for a worked
    example (a small ring fixture) and :func:`_net_label_positions`'s own
    docstring for the full rationale. Purely additive -- every existing
    ``nets[]`` consumer that reads only ``name``/``pin``/``device_count`` is
    unaffected.

    ``device_classes`` is what the *deck* is structurally capable of
    recognising (:attr:`klayout_tools.decks.ExtractionDeck.device_classes`)
    -- independent of what this particular layout happens to contain, unlike
    ``device_counts`` (issue #221). A consumer that needs to know ahead of
    time whether a deck can even produce a given device class (e.g. before
    pairing it with a reference netlist for ``klt lvs``) reads this field
    instead of inferring "not supported" from a zero count.

    ``metrics`` (issue #1848, adopting the declared metric namespace
    registry from #247 beyond its ``klt layout-metrics``/``klt drc`` (#1847)
    adopters) is a **parallel, additive** object re-keying this response's
    fixed, non-caller-supplied numeric fields under their declared
    METRICS2.1-style names from :mod:`klayout_tools.metrics`'s registry --
    ``device_count`` -> ``extract__device__count``, ``net_count`` ->
    ``extract__net__count``, ``pin_count`` -> ``extract__pin__count``, and,
    only when ``--parasitics`` was given, ``parasitics.r_count`` ->
    ``extract__resistor__count``, ``parasitics.c_count`` ->
    ``extract__capacitor__count``, ``parasitics.cc_count`` ->
    ``extract__coupling__capacitor__count``, ``parasitics.l_count`` ->
    ``extract__inductor__count``, ``parasitics.total_resistance_ohm`` ->
    ``extract__resistance__ohm``, ``parasitics.total_capacitance_ff`` ->
    ``extract__capacitance__ff``,
    ``parasitics.total_coupling_capacitance_ff`` ->
    ``extract__coupling__capacitance__ff``, and
    ``parasitics.total_inductance_nh`` -> ``extract__inductance__nh``. It
    never replaces or changes any of those fields, which stay exactly as
    documented; ``metrics`` always carries the three non-parasitics entries,
    and additionally carries the eight parasitics entries only when
    ``parasitics`` above is non-``None``. Every entry declares
    ``aggregator="sum"`` and ``higher_is_better=None`` (a purely structural/
    physical count or total, not itself a "better or worse" axis -- see
    ``docs/design/metric-namespace.md``'s polarity reasoning, applied
    identically here). ``device_counts`` (the per-device-class dict) and the
    other structural/diagnostic report shapes (``substrate_dc_tie``,
    ``metals_without_coefficient``, ``overlap_pairs_without_coefficient``,
    etc. -- dicts/lists, not scalar metrics) are deliberately **not**
    declared in this registry pass: the registry's ``{aggregator,
    higher_is_better, critical}`` tuple is defined for a single scalar
    value, not a dict keyed by an open-ended, deck-defined device-class
    vocabulary (the same reasoning issue #247 documented for ``klt sim``'s
    caller-supplied ``measurements[]`` names -- see
    ``docs/design/metric-namespace.md``'s "Follow-on work" section).

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

    # `--parasitics-net` (issue #1700) requires `--parasitics`: it scopes the
    # lumped-RC ground pass itself, so without that pass there is nothing to
    # scope -- same reasoning as `--critical-net` immediately above.
    parasitics_nets_set = frozenset(parasitics_nets) if parasitics_nets else None
    if parasitics_nets_set is not None and not parasitics:
        raise ExtractError("--parasitics-net requires --parasitics")

    # `--parasitics-top-cell-only` (issue #1704) requires `--parasitics`:
    # it splits the same lumped-RC ground pass this flag piggybacks on into
    # a top-cell-drawn/instance-drawn share -- same reasoning as
    # `--parasitics-net`/`--critical-net` above.
    if parasitics_top_cell_only and not parasitics:
        raise ExtractError("--parasitics-top-cell-only requires --parasitics")

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
        net_label_positions,
        device_instance_paths,
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
        parasitics_nets=parasitics_nets_set,
        parasitics_top_cell_only=parasitics_top_cell_only,
        def_pins=def_pins,
        pin_source_cells=pin_source_cells,
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
        devices, device_counts = _describe_devices(circuit, device_instance_paths)
        nets = _describe_nets(circuit, net_label_positions)
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
        # Additive field (issue #1700): echoes the `--parasitics-net` request
        # back verbatim (as given, not sorted/deduplicated) -- `[]` when the
        # flag was never given. Without it a scoped run's deliberately-short
        # `parasitics.nets[]` would be indistinguishable from a layout whose
        # other nets genuinely had no ground-eligible geometry.
        parasitics_report["parasitics_nets"] = (
            list(parasitics_nets) if parasitics_nets else []
        )
        if parasitics_nets_set:
            # Same escaped-spelling comparison `critical_nets` makes below
            # (issue #1162): `ground_nets[].net` is the unescaped identity
            # spelling, the caller-supplied set is the escaped one.
            scoped_matched_names = {
                spice_safe_net_name(entry["net"]) for entry in ground_nets
            }
            unscoped = sorted(parasitics_nets_set - scoped_matched_names)
            if unscoped:
                warnings.append(
                    "--parasitics-net name(s) "
                    f"{', '.join(repr(name) for name in unscoped)} match no "
                    "net with ground-eligible parasitics geometry in this "
                    "layout -- no R/C was computed for them. See "
                    "docs/cli/extract.md's '--parasitics-net' section."
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
            # `--parasitics-net` (issue #1700) scoped the ground pass that
            # `matched_net_names` is derived from, so a critical net left out
            # of that scope is absent for a reason the generic "matches no
            # net in this layout" wording below would misattribute to the
            # layout. Reported separately, and excluded from that warning, so
            # the caller is pointed at the flag combination actually
            # responsible. Empty (and therefore inert) unless the new flag
            # was given.
            scoped_out_critical = (
                sorted(critical_nets_set - parasitics_nets_set)
                if parasitics_nets_set
                else []
            )
            if scoped_out_critical:
                warnings.append(
                    "--critical-net name(s) "
                    f"{', '.join(repr(name) for name in scoped_out_critical)} "
                    "were not also named --parasitics-net -- that flag scopes "
                    "the ground R/C pass lateral coupling is measured from, "
                    "so no lateral coupling was computed for them. See "
                    "docs/cli/extract.md's '--parasitics-net' section."
                )
            unmatched = sorted(
                critical_nets_set - matched_net_names - set(scoped_out_critical)
            )
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
        # Additive field (issue #1704): `True` only when
        # `--parasitics-top-cell-only` was given -- distinguishes a run whose
        # `nets[].resistance_ohm_top_cell`/`.capacitance_ff_top_cell` are
        # real computed splits from one where they are `None` because the
        # flag was never asked for.
        parasitics_report["top_cell_only"] = bool(parasitics_top_cell_only)
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

    # Issue #1503: every synthesized substrate identity `_tie_substrate_nets_
    # to_ground` just tied to ground (`substrate_dc_tie["nets"]`, already
    # computed above as part of `parasitics_report`) is declared SPICE-global
    # via `.GLOBAL` -- see `create_model_binding_delegate`'s `global_nets`
    # docstring paragraph and `_tie_substrate_nets_to_ground`'s own docstring
    # for why. `[]` (the pre-#1503 default) whenever `--parasitics` was not
    # given or synthesized no substrate identity at all, in which case
    # `create_model_binding_delegate`'s `write_header` override emits
    # nothing and this is a no-op.
    substrate_global_nets: list[str] = []
    if parasitics_report is not None:
        substrate_global_nets = [
            entry["net"] for entry in parasitics_report["substrate_dc_tie"]["nets"]
        ]
    writer = kdb.NetlistSpiceWriter(
        create_model_binding_delegate(
            model_bindings if model_bindings is not None else {},
            global_nets=substrate_global_nets,
        )
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

    # Additive `metrics` block (issue #1848, adopting the declared metric
    # namespace registry from #247 beyond its `layout-metrics`/`klt drc`
    # (#1847) adopters) -- see `run_extract`'s docstring "metrics" paragraph
    # for the full contract. Never replaces `device_count`/`net_count`/
    # `pin_count`/`parasitics.*` above, which stay exactly as documented.
    metrics: dict[str, int | float] = {
        field_metric_name: result[field_name]
        for field_name, field_metric_name in _METRIC_NAME_BY_FIELD.items()
    }
    if parasitics_report is not None:
        metrics.update(
            {
                field_metric_name: parasitics_report[field_name]
                for field_name, field_metric_name in (
                    _PARASITICS_METRIC_NAME_BY_FIELD.items()
                )
            }
        )
    result["metrics"] = metrics

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


# `\$<n>` -- the escaped spelling `spice_safe_net_name` gives every anonymous
# net (issue #1162) wherever a net name enters the JSON report
# (`nets[].name`, `devices[].nets[...]`, `parasitics.nets[].net`/`hub_net`,
# etc.) -- the bookkeeping spelling issue #1559's field-class table
# (`docs/cli/extract.md`) documents as not a stable contract across builds.
_ANONYMOUS_NET_NAME_RE = re.compile(r"^\\\$\d+$")


def _net_attachment_key(net_name: str, devices: list[Any]) -> str:
    """The sorted ``<device>.<terminal>`` attachment list for ``net_name``,
    read from this same report's already-built ``devices[]`` array -- the
    structural identity issue #1559 normalizes an anonymous net's unstable
    ``\\$<n>`` spelling to, mirroring the "resolve by structural identity,
    not the raw counter" convention :func:`_net_identity_name`'s docstring
    and the ``net_id``-keyed lookups at lines 483-491/610-617 already follow
    for the *live* ``kdb.Net``/``kdb.Device`` objects, applied here instead
    to the already-serialized JSON report. Returns ``""`` for a net with no
    device terminal at all (real floating geometry, or a synthesized
    ground/substrate net) -- nothing to disambiguate by, so the caller
    leaves that net's original spelling untouched rather than fabricate a
    key that cannot be trusted to be unique.
    """
    attachments = sorted(
        f"{device.get('name')}.{terminal}"
        for device in devices
        if isinstance(device, dict)
        for terminal, terminal_net in (device.get("nets") or {}).items()
        if terminal_net == net_name
    )
    return ",".join(attachments)


def _anonymous_net_name_map(report: Mapping[str, Any]) -> dict[str, str]:
    """Build the ``{raw \\$<n> spelling: canonical structural name}`` map
    this report's own ``nets[]``/``devices[]`` arrays imply -- every
    anonymous net whose attachment list (see :func:`_net_attachment_key`) is
    non-empty maps to a name derived from that list; every other net (named
    or unresolvably anonymous) is absent from the map, i.e. left unchanged
    by :func:`_rewrite_net_name`.
    """
    nets = report.get("nets")
    devices = report.get("devices")
    if not isinstance(nets, list) or not isinstance(devices, list):
        return {}
    name_map: dict[str, str] = {}
    for entry in nets:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not _ANONYMOUS_NET_NAME_RE.match(name):
            continue
        attachment_key = _net_attachment_key(name, devices)
        if attachment_key:
            name_map[name] = f"$anon<{attachment_key}>"
    return name_map


def _rewrite_net_name(value: Any, name_map: Mapping[str, str]) -> Any:
    """Canonicalize a single report string that may *be* an anonymous net
    name, or may *start with* one followed by a suffix this module mints
    from it (a parasitics leg/hub/segment net, e.g. ``\\$3__t0``) -- returns
    ``value`` unchanged for anything else (not a string, or a string not
    built from a key in ``name_map``).

    The suffix case requires a boundary check (the character immediately
    after the matched prefix must not be alphanumeric) so that renaming
    ``\\$3`` never also matches the unrelated net ``\\$31`` by accident.
    """
    if not isinstance(value, str):
        return value
    canonical = name_map.get(value)
    if canonical is not None:
        return canonical
    for raw_name, canonical_name in name_map.items():
        if not value.startswith(raw_name):
            continue
        boundary = value[len(raw_name) : len(raw_name) + 1]
        if boundary and boundary.isalnum():
            continue
        return canonical_name + value[len(raw_name) :]
    return value


def _canonicalize_extract_report_for_rerun_diff(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a deep copy of an extract ``--format json`` report with every
    field ``docs/cli/extract.md``'s field-class table (issue #1559)
    classifies as **bookkeeping** -- extractor-internal identifiers with no
    meaning outside the one run that produced them -- canonicalized so
    ``klt extract --check <report> --rerun`` stops reporting bookkeeping-only
    differences as ``status: "drifted"``:

    - every ``net_id`` (``Net.cluster_id``, KLayout's own internal counter)
      is stripped wherever it appears, at any nesting depth -- it is never
      itself part of the extracted *content*, only a same-run handle onto a
      net object (see this module's own many "resolved by net_id, not by
      name" docstrings, e.g. :func:`_net_identity_name`,
      :func:`_mom_ground_entry_for_crosscheck`);
    - every anonymous ``\\$<n>`` net-name spelling is rewritten to the
      structural identity :func:`_anonymous_net_name_map` derives for it
      (that net's own sorted ``<device>.<terminal>`` attachment list), so
      two builds that assign the *same* net a *different* placeholder
      number compare equal;
    - ``nets[]``/``parasitics.nets[]`` -- whose extraction-time sort key is
      itself derived from the (possibly still-anonymous, pre-canonicalization)
      name -- are re-sorted by ``(canonical name, structural attachment
      key)``, so a pure reordering artifact of that unstable sort key does
      not itself register as drift. The structural attachment key (the same
      sorted ``<device>.<terminal>`` list :func:`_anonymous_net_name_map`
      derives an anonymous net's canonical name from) is also needed as a
      *tiebreaker* among two or more entries that legitimately share one
      ``name``/``net`` (e.g. several distinct, un-strapped islands with the
      identical layout label, issue #765/#811, which is exactly what
      ``net_id`` used to disambiguate before this function stripped it) --
      without it, two same-named entries would sort ambiguously and the
      list order between committed/fresh could itself register as
      (spurious) drift.

    A genuine content change is never swallowed by this: a differing
    ``resistance_ohm``/``capacitance_ff``/device count/pin connection etc.
    survives both the ``net_id`` strip (a different field) and the name
    rewrite (canonicalization is a pure relabeling, not a value change), so
    :func:`~klayout_tools._report_verify.diff_verdict_fields` still reports
    it.

    Used only to build the two dicts :func:`rerun_extract_report` diffs --
    the *embedded* ``fresh`` report in its response is always the real,
    un-normalized one, so a consumer inspecting it still sees the tool's
    actual current output verbatim.
    """
    name_map = _anonymous_net_name_map(report)
    normalized = _strip_net_id_and_rewrite_names(report, name_map)

    nets = normalized.get("nets")
    devices = normalized.get("devices")
    if isinstance(nets, list) and isinstance(devices, list):
        normalized["nets"] = sorted(
            nets,
            key=lambda entry: (
                (
                    entry.get("name", ""),
                    _net_attachment_key(entry.get("name", ""), devices),
                )
                if isinstance(entry, dict)
                else ("", "")
            ),
        )

    parasitics = normalized.get("parasitics")
    if isinstance(parasitics, dict):
        parasitics_nets = parasitics.get("nets")
        if isinstance(parasitics_nets, list):
            parasitics["nets"] = sorted(
                parasitics_nets,
                key=lambda entry: (
                    (
                        entry.get("net", ""),
                        _parasitics_net_attachment_key(entry),
                    )
                    if isinstance(entry, dict)
                    else ("", "")
                ),
            )

    return normalized


def _parasitics_net_attachment_key(entry: Mapping[str, Any]) -> str:
    """The sorted ``<device>.<terminal>`` attachment list a single
    ``parasitics.nets[]`` entry's own ``terminals[]`` already names --
    :func:`_canonicalize_extract_report_for_rerun_diff`'s sort tiebreaker
    for two entries sharing one ``net`` (see that function's docstring),
    built directly from the entry itself rather than re-deriving it from
    the top-level ``devices[]`` array the way :func:`_net_attachment_key`
    does for a top-level ``nets[]`` entry."""
    terminals = entry.get("terminals")
    if not isinstance(terminals, list):
        return ""
    return ",".join(
        sorted(
            f"{terminal.get('device')}.{terminal.get('terminal')}"
            for terminal in terminals
            if isinstance(terminal, dict)
        )
    )


def _strip_net_id_and_rewrite_names(value: Any, name_map: Mapping[str, str]) -> Any:
    """Recursively rebuild ``value`` (a JSON-decoded report, or any nested
    piece of one), dropping every ``net_id`` key and passing every string
    leaf through :func:`_rewrite_net_name` -- the two bookkeeping
    normalizations :func:`_canonicalize_extract_report_for_rerun_diff`
    applies report-wide rather than at a fixed set of paths, so a new report
    field carrying a net name or a ``net_id`` needs no separate registration
    here to be covered."""
    if isinstance(value, dict):
        return {
            key: _strip_net_id_and_rewrite_names(val, name_map)
            for key, val in value.items()
            if key != "net_id"
        }
    if isinstance(value, list):
        return [_strip_net_id_and_rewrite_names(item, name_map) for item in value]
    return _rewrite_net_name(value, name_map)


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

    The diff itself compares :func:`_canonicalize_extract_report_for_rerun_diff`'s
    output for each side, not ``committed``/``fresh`` verbatim (issue
    #1559): ``net_id``, anonymous ``$N`` net-name spellings, and
    ``parasitics.nets[]`` ordering are extractor-internal bookkeeping with no
    contract across builds (``docs/cli/extract.md``'s field-class table), so
    a committed/fresh pair differing *only* in those no longer reports
    ``status: "drifted"``. This is a relabeling for comparison purposes
    only -- the embedded ``fresh`` in the response below is always the real,
    un-normalized report.

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
    return build_rerun_result(
        report_path=report_path,
        committed=committed,
        fresh=fresh,
        committed_for_diff=_canonicalize_extract_report_for_rerun_diff(committed),
        fresh_for_diff=_canonicalize_extract_report_for_rerun_diff(fresh),
    )


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
    parasitics_nets: frozenset[str] | None = None,
    parasitics_top_cell_only: bool = False,
    def_pins: frozenset[str] | None = None,
    pin_source_cells: frozenset[str] | None = None,
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
    dict[int, list[dict[str, Any]]],
    dict[int, list[dict[str, Any]]],
]:
    """Core extraction: read ``path``, resolve ``deck_name`` and the top
    cell, and run flat device + connectivity extraction. Returns
    ``(netlist, top_cell_name, dbu_um, warnings, parasitic_nets,
    black_box_regions, dummy_devices_dropped, unmodelled_poly,
    voltage_domain_warnings, abstracted_cells, dead_metal, mom_crosscheck,
    net_label_positions, device_instance_paths)`` -- see
    :func:`_extract_netlist`'s own docstring for ``net_label_positions``
    (issue #1540) and ``device_instance_paths`` (issue #1666).

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

    ``parasitics_top_cell_only`` (issue #1704): forwarded to
    ``_extract_netlist``/:func:`_compute_parasitics` -- ``True`` additionally
    splits each net's ground R/C into the portion drawn directly in the top
    cell versus the portion drawn inside an instantiated sub-block, reported
    as the additive ``resistance_ohm_top_cell``/``capacitance_ff_top_cell``
    ground-entry fields. Off by default -- byte-identical to this flag's
    pre-#1704 behavior. See :func:`_compute_parasitics`'s docstring for the
    algorithm and :func:`run_extract` for the full CLI contract.

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

    ``pin_source_cells`` (issue #1513): a *positional* counterpart to
    ``declared_pins``/``def_pins`` for a `klt gen-compose`d assembly with
    no governing top-level DEF -- forwarded to :func:`_extract_netlist`,
    which keeps a promoted net whenever it resolves, by probing a label's
    own composed-frame position (not by matching its string), from a text
    shape drawn inside one of these named cells. ``None`` skips this
    reconciliation. See :func:`run_extract` for the full rationale.

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
    abstract_cell_local_candidates: dict[
        int, dict[str, list[tuple[kdb.Point, str]]]
    ] = {}
    abstract_cell_global_net_ports: dict[int, int] = {}
    abstract_body_identity_cover: tuple[kdb.Region, kdb.Region] | None = None
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
            # Also computed *before* erasure (issue #1911): the well /
            # substrate-isolation cover the matched instances contribute
            # (`_abstract_cell_body_identity_cover` -- reunioned into
            # `_extract_netlist`'s whole-layout *classification* split so
            # erasing a black box's well cannot silently reclassify a tie
            # drawn outside it onto the deck's global substrate net), and
            # the per-cell-type count of ports that only ever resolve
            # through that global (`_abstract_cell_global_net_ports` --
            # warned about instead of silently dropped).
            abstract_body_identity_cover = _abstract_cell_body_identity_cover(
                layout, deck, abstract_instances
            )
            abstract_cell_global_net_ports = {
                cell_index: _abstract_cell_global_net_ports(
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
        net_label_positions,
        device_instance_paths,
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
        abstract_cell_global_net_ports=abstract_cell_global_net_ports,
        abstract_body_identity_cover=abstract_body_identity_cover,
        mom_net=mom_net,
        mom_background_permittivity=mom_background_permittivity,
        def_net_names=def_net_names,
        critical_nets=critical_nets,
        parasitics_nets=parasitics_nets,
        parasitics_top_cell_only=parasitics_top_cell_only,
        def_pins=def_pins,
        pin_source_cells=pin_source_cells,
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
        net_label_positions,
        device_instance_paths,
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
                    # expanded_name() yields a bare $<n> for anonymous devices:
                    # deliberately NOT backslash-escaped, unlike net names. A
                    # device name always follows a class-letter prefix (M$1, R$22)
                    # on its SPICE instance line, so ngspice's leading-$ comment
                    # hazard that spice_safe_net_name() guards against cannot
                    # arise here. See issue #1439 and docs/cli/extract.md.
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


def _duplicated_declared_pin_names(
    netlist: kdb.Netlist, top_name: str, declared: frozenset[str]
) -> list[str]:
    """Return the sorted, de-duplicated subset of ``declared`` whose
    any-component-label match (issue #1390/#1687) hits 2+ *physically
    disconnected* nets in the top circuit's currently-promoted pins --
    issue #2000.

    ``declared_pins``/``def_pins`` demote by net *name*
    (``_reconcile_top_pins``), so when two disconnected nets happen to carry
    the identical drawn text label and that shared name is declared, both
    independently survive their own reconciliation pass: each is a
    genuinely distinct electrical node (a different ``Net.cluster_id``), not
    a duplicate reading of the same one. Demoting either would risk silently
    hiding a real split-net connectivity defect from a downstream `klt lvs`
    reference netlist -- the same trade-off `ignored_layers[]`'s own
    "extracts as multiple disconnected nets instead of one" warning
    documents for an undeclared-connectivity-layer split, a few hundred
    lines up in :func:`run_extract`. So this does not change which nets are
    promoted -- it only lets a caller see that a single declared name now
    maps to more than one promoted pin, which is otherwise invisible short
    of diffing the written ``.SUBCKT`` port list for KLayout's own
    ``$1``-suffixed disambiguation of the repeated net name.

    Call *after* the caller's own demotion pass for ``declared`` has already
    run, so this only sees nets that are still promoted pins.
    """
    circuit = netlist.circuit_by_name(top_name)
    if circuit is None or not declared:
        return []

    name_net_ids: dict[str, set[int]] = {}
    for pin in circuit.each_pin():
        net = circuit.net_for_pin(pin.id())
        if net is None or not net.name:
            continue
        for component in net.name.split(","):
            if component in declared:
                name_net_ids.setdefault(component, set()).add(net.cluster_id)

    return sorted(name for name, ids in name_net_ids.items() if len(ids) > 1)


def _pin_source_cell_net_names(
    l2n: kdb.LayoutToNetlist,
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    poly: kdb.Region,
    nwell: kdb.Region,
    tap: kdb.Region,
    metals: list[kdb.Region],
    cell_names: frozenset[str],
) -> tuple[set[str], list[str]]:
    """Resolve every drawn pin-name label physically located inside an
    instance of one of ``cell_names`` -- anywhere in ``top_cell``'s
    hierarchy, at any depth -- to the actual extracted net at that exact
    position (issue #1513).

    This is the *positional* counterpart to ``declared_pins``/``def_pins``'s
    *string* matching against a promoted net's own (possibly comma-joined)
    name. Both of those defeat KLayout's flat-extraction convention of
    joining every text label found on one electrical net into a single
    ``Net.name`` in a different way: ``--pins``'s own comma item-separator
    collides with that join's separator (a net named ``A,CLK`` can never be
    spelled as one ``--pins`` token), and ``--def-pins``'s component-match
    fallback (``set(name.split(",")) & def_pins``, below) is *too*
    permissive once two independently-labelled macros are composed --
    literally the same declared component string (e.g. ``CLK``) drawn by
    two unrelated macros' own internal, generic labels keeps *both* nets
    promoted, with no way to tell "the net whose only relevant label is
    this one" from "any net carrying this label as one of several".

    ``cell_names`` sidesteps both failure modes by identifying a **specific
    physical label**, not a name: for each of ``deck``'s own label layers
    (``well_label``/``poly_label``/``metal_labels``), every text shape drawn
    *anywhere* under ``top_cell`` -- via ``Cell.begin_shapes_rec``, exactly
    like ``_label_layer_strings(recursive=True)`` -- whose *immediate owning
    cell* (``RecursiveShapeIterator.cell()``) has a name in ``cell_names`` is
    probed at its own composed-frame position
    (``RecursiveShapeIterator.trans()`` applied to the label's local
    position, then ``LayoutToNetlist.probe_net`` on the label layer's own
    conductor region -- ``nwell``/``tap`` for ``well_label``, ``poly`` for
    ``poly_label``, ``metals[i]`` for ``metal_labels[i]``) to recover the
    *real* ``kdb.Net`` object that label names, independent of what string
    it happens to spell or what other, unrelated net elsewhere in the
    layout happens to carry the same string.

    A caller names the cell(s) a `gen-compose`d assembly's own hand-drawn
    interconnect script draws its top-level pin labels into (e.g. the
    ``f"{block_id}__{src_cell_name}"`` sub-cell `klt gen-compose` itself
    creates for a composed-in block, or a hand-authored interconnect
    block's own top cell) -- unlike ``--top-cell-pins``, this deliberately
    *does not* require that cell to be ``top_cell`` itself, since a
    composed assembly's own interconnect labels necessarily live in an
    *instanced* sub-cell, not literally in the new top cell's own shapes
    (issue #291's own below-top-label heuristic demotes them for exactly
    this reason).

    Returns ``(promoted_names, unresolved_labels)``: ``promoted_names`` is
    the set of ``Net.name`` values reached this way (a name that also
    happens to carry other, unrelated joined labels is still kept whole --
    ``_reconcile_top_pins``'s keep-list matches on the full name, same as
    ``declared_pins``); ``unresolved_labels`` is every label string found in
    a named cell that resolved to no conductor at its own position at all
    (a label drawn with no underlying drawn shape, or one erased by a
    black-box/abstract-cell mask), sorted and de-duplicated, for the
    caller's own warning -- mirroring ``def_pins``'s "matched no promoted
    net" report.

    Empty ``cell_names`` -- and thus the whole ``pin_source_cells``
    reconciliation this feeds -- returns ``(set(), [])`` (a no-op keep-list,
    meaning "demote everything"), never called by
    :func:`_extract_netlist` for a ``None`` ``pin_source_cells`` in the
    first place (byte-identical-default invariant, same as every other
    declared-pin mechanism above).
    """
    import klayout.db as kdb

    if not cell_names:
        return set(), []

    probe_targets: list[tuple[tuple[int, int] | None, list[kdb.Region]]] = [
        (deck.well_label, [nwell, tap]),
        (deck.poly_label, [poly]),
    ] + [
        (layer, [metals[index]])
        for index, layer in enumerate(deck.metal_labels)
        if index < len(metals)
    ]

    promoted_names: set[str] = set()
    unresolved_labels: set[str] = set()
    for layer_pair, probe_regions in probe_targets:
        if layer_pair is None:
            continue
        layer_index = layout.find_layer(*layer_pair)
        if layer_index is None:
            continue
        iterator = top_cell.begin_shapes_rec(layer_index)
        while not iterator.at_end():
            shape = iterator.shape()
            if shape.is_text() and iterator.cell().name in cell_names:
                text_string = shape.text_string
                local = shape.text_trans.disp
                point = iterator.trans() * kdb.Point(local.x, local.y)
                net = None
                for region in probe_regions:
                    net = l2n.probe_net(region, point)
                    if net is not None:
                        break
                if net is not None and net.name:
                    promoted_names.add(net.name)
                else:
                    unresolved_labels.add(text_string)
            iterator.next()

    return promoted_names, sorted(unresolved_labels)


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

    **Restore-matching is by ``cluster_id``, not by ``name`` (issue #1540).**
    Two or more distinct, device-free pinned nets on the *same* circuit can
    legitimately share one ``name`` string -- the exact "flat extraction of a
    layout with internally-repeated sub-cells" collision this issue reports
    (e.g. a ring of identical stages, each junction node carrying no device
    of its own). Matching the restore loop's "does this net already exist"
    check by ``name`` (as this function did before #1540) collapses every
    later same-named survivor onto the *first* one restored: the second
    survivor's own ``cluster_id`` is silently discarded, the two distinct
    islands merge into one recreated ``kdb.Net`` object, and one of the two
    real, physically-separate nets vanishes from the response entirely --
    not merely mis-labelled, genuinely gone. Verified directly against
    ``klayout.db`` before this fix: a device-free two-net-one-name fixture
    (mirroring this function's own bond-pad/seal-ring/RDL motivating case)
    extracted only one of the two nets. Matching by ``cluster_id`` instead
    -- unique across every net *object* on a circuit, the same identity
    :func:`_compute_parasitics`'s ``net_id`` and this issue's own
    ``nets[].net_id`` already rely on for this exact "several distinct nets,
    one label" shape (issue #765/#811) -- restores each survivor to its own
    distinct net regardless of how many others share its name.
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

        # Matched by `cluster_id` when it is a real (non-zero) identity,
        # falling back to the pre-#1540 by-`name` match only for the
        # `cluster_id == 0` sentinel (issue #1540) -- see this function's own
        # docstring for why a name-keyed match silently collapses distinct
        # same-named survivors onto one recreated net. `cluster_id == 0`
        # means "never tied to a real `LayoutToNetlist` cluster" (see
        # `_compute_parasitics`'s own `cluster_id == 0` guard) -- true for
        # every net a hand-built `kdb.Netlist` creates directly (as opposed
        # to one `l2n.extract_netlist()` produced), where several genuinely
        # distinct nets can share that same `0` default; matching those by
        # `cluster_id` alone would reintroduce the identical "several
        # distinct nets collapse onto one" bug this fix exists to close, just
        # keyed on `0` instead of on a name string.
        if cluster_id != 0:
            net = next(
                (n for n in circuit.each_net() if n.cluster_id == cluster_id), None
            )
        else:
            net = next(
                (
                    n
                    for n in circuit.each_net()
                    if n.name == net_name and n.cluster_id == 0
                ),
                None,
            )
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


def _dotted_rename_display(name: str) -> str:
    """:func:`spice_safe_net_name`'s reporting spelling with its ``.`` ->
    ``_`` rewrite (issue #2145) deliberately *left out* -- the only correct
    way to show the "before" side of a :func:`_rewrite_dotted_net_names`
    rename in a ``warnings[]`` message.

    Passing the pre-rename name through ``spice_safe_net_name`` itself would
    apply the very rewrite the message is reporting, printing a useless
    ``X_y -> X_y``; printing the raw ``Net.expanded_name()`` instead would
    show a comma-joined (``X.y,Z``) or bare-``$`` spelling that appears in no
    other artifact. This keeps the other two rewrites so both sides of the
    arrow read in the same namespace as ``nets[].name``.
    """
    shown = name.replace(",", "|")
    return "\\" + shown if shown.startswith("$") else shown


#: How many `before -> after` examples a `_dotted_rename_warnings` entry
#: spells out before collapsing the rest into a `+<n> more` tail -- the same
#: "show a handful, count the rest" shape `--def-net-names`' own unresolved
#: -name warning already uses.
_DOTTED_RENAME_WARNING_EXAMPLES = 5


def _dotted_rename_warnings(renames: list[tuple[str, str, str]]) -> list[str]:
    """Zero or one ``warnings[]`` entry disclosing what
    :func:`_rewrite_dotted_net_names` renamed (issue #2145).

    Returns a list (empty when nothing was renamed, which is the usual case)
    so the call site in :func:`_extract_netlist` stays a single unconditional
    ``warnings.extend(...)`` rather than another branch in an already very
    long function.

    The rename is deliberately loud rather than silent: a caller holding a
    net name from *outside* this run -- a DEF, a schematic, a previous
    report, a hand-written ``--critical-net`` argument -- needs to know its
    dotted spelling no longer appears in any artifact.
    """
    if not renames:
        return []
    shown = renames[:_DOTTED_RENAME_WARNING_EXAMPLES]
    examples = ", ".join(
        f"{_dotted_rename_display(before)} -> {spice_safe_net_name(after)}"
        for _circuit, before, after in shown
    )
    more = len(renames) - len(shown)
    count_phrase = (
        "1 net carried a hierarchical name"
        if len(renames) == 1
        else f"{len(renames)} nets carried hierarchical names"
    )
    verb = "was" if len(renames) == 1 else "were"
    return [
        f"{count_phrase} containing '.' (ngspice's own hierarchy separator, "
        f"which makes the node unaddressable by its written name) and "
        f"{verb} renamed with '_' instead ({examples}"
        + (f", +{more} more" if more > 0 else "")
        + ") -- nets[], the written netlist, the SPEF and any `klt lvs` "
        "output all use the renamed spelling, and so must "
        "--critical-net/--distributed-rc/--mom-net/--mom-rlc-net"
    ]


def _rewrite_dotted_net_names(netlist: kdb.Netlist) -> list[tuple[str, str, str]]:
    """Rename every net whose name contains ngspice's hierarchy separator
    (``.``) to the SPICE-addressable ``_`` spelling
    :func:`~klayout_tools.extract_parasitics.spice_safe_net_name` reports
    (issue #2145). Returns the ``(circuit, before, after)`` triples it
    renamed, in circuit/net iteration order (empty -- and the netlist
    untouched -- for the overwhelming majority of layouts, whose net names
    carry no dot at all).

    **Why the rename has to happen on the real ``kdb.Net``.** The two other
    SPICE-hazard characters this repo reconciles between its JSON report and
    its written netlist (a merged-label ``,``, issue #696; a leading ``$``,
    issue #1162) are escaped by ``NetlistSpiceWriter`` *itself*, so
    ``spice_safe_net_name`` only has to *predict* what the writer will emit.
    ``.`` is different: confirmed against a live ``NetlistSpiceWriter`` run,
    a net named ``XBIAS.vb1`` writes verbatim as ``XBIAS.vb1`` in the
    ``.SUBCKT`` pin list and on every device card. Since ``.`` is ngspice's
    own hierarchy separator, that token is then read as a path expression
    (instance ``XBIAS`` -> node ``vb1``) wherever a node reference is
    parsed: the node is unaddressable by its written name in
    ``v()``/``.meas``/``.ic``, and the same token means one thing in the
    ``.SUBCKT`` header and another in a probe directive. The only way to
    make the *written* netlist safe is to change what the net is called
    before the writer sees it.

    Dot-qualified names are not hypothetical: ``klt place-and-route``'s DEF
    net names (replayed onto the extracted nets by ``--def-net-names``,
    issue #951) spell a sub-instance's internal net with its instance path,
    and a drawn label may carry the same convention. They are exactly the
    internal nodes a ``--parasitics`` post-layout run wants to probe.

    **Where this runs, and what it therefore covers.** Called from
    :func:`_extract_netlist` immediately after the purge pass and *before*
    anything reads a net name: the ``devices[]``/``nets[]`` report,
    ``merged_net_labels[]``, ``_compute_parasitics``/``_inject_parasitics``
    (whose synthesized leg/hub nets are named off their parent net, so they
    inherit the safe spelling rather than needing a second pass), the SPEF
    writer, ``klt lvs``'s ``net_correspondence``/``mismatches[].net``, and
    the written netlist all see one spelling. It touches **only** net names
    -- a SPICE dot-command (``.SUBCKT``/``.ENDS``/``.GLOBAL``) and a numeric
    literal (``L=0.28U``) are emitted by the writer from entirely different
    inputs and are structurally out of reach of this pass.

    **Collisions are resolved per-circuit, not per-name.** No rewrite that
    leaves dot-free names alone can be injective (the dot-free namespace is
    already fully occupied), so ``a.b`` can land on a pre-existing ``a_b``,
    and ``a.b_c``/``a_b.c`` can land on each other. Each circuit's existing
    names are tracked as the renames are applied, and a candidate that is
    already taken gets the smallest ``_<n>`` suffix that is not -- so no two
    distinct nets ever share a name in one written netlist. Renaming in
    ``each_net()`` order makes the outcome deterministic for a given
    circuit.

    Because ``--critical-net``/``--distributed-rc``/``--mom-net``/
    ``--mom-rlc-net`` match against the post-rename namespace (they run
    inside ``_compute_parasitics``, after this pass), a dot-qualified net
    must be named to those flags in the rewritten spelling -- the same
    spelling ``nets[].name`` reports. See ``docs/cli/extract.md``'s
    "Hierarchical net names are dot-free".
    """
    renamed: list[tuple[str, str, str]] = []
    for circuit in netlist.each_circuit():
        taken = {net.expanded_name() for net in circuit.each_net()}
        # Snapshot first: renaming while iterating `each_net()` mutates the
        # collection the iterator is walking.
        dotted = [
            (net, net.expanded_name())
            for net in circuit.each_net()
            if SPICE_HIERARCHY_SEPARATOR in net.expanded_name()
        ]
        for net, before in dotted:
            # The name being replaced stops occupying the namespace, so a
            # net can keep an unsuffixed candidate that only collided with
            # its own former spelling.
            taken.discard(before)
            candidate = before.replace(
                SPICE_HIERARCHY_SEPARATOR, SPICE_SAFE_HIERARCHY_JOIN
            )
            if candidate in taken:
                base = candidate
                index = 1
                while f"{base}{SPICE_SAFE_HIERARCHY_JOIN}{index}" in taken:
                    index += 1
                candidate = f"{base}{SPICE_SAFE_HIERARCHY_JOIN}{index}"
            taken.add(candidate)
            net.name = candidate
            renamed.append((circuit.name, before, candidate))

        # Pin names are cosmetic (`NetlistSpiceWriter` writes the *net*
        # name in the `.SUBCKT` header and on every instance line when
        # `use_net_names` is set -- a pin's own name only reaches the
        # leading `* pin ...` comment), but `make_top_level_pins()` names a
        # promoted pin after its net, so leaving them behind would print a
        # dotted `* pin` comment above a dot-free `.SUBCKT` line.
        for pin in circuit.each_pin():
            pin_net = circuit.net_for_pin(pin.id())
            if pin_net is None or not pin.name():
                continue
            if SPICE_HIERARCHY_SEPARATOR not in pin.name():
                continue
            circuit.rename_pin(pin.id(), pin_net.expanded_name())
    return renamed


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


def mom_capacitor_device_class(name: str) -> kdb.DeviceClass:
    """Build the ``kdb.DeviceClass`` one
    :class:`~klayout_tools.decks.MomCapacitorDevice` entry named ``name``
    registers -- two terminals ``A``/``B`` (declared
    EQUIVALENT, order is arbitrary -- see ``MomCapacitorDevice``'s
    docstring), plus ``W``/``L`` geometry parameters and deliberately no
    capacitance parameter at all (the real device's ``C`` is supplied by the
    SPICE/Verilog-A model, not computed here -- see ``docs/json-contract
    .md``'s "MoM capacitor devices" note for the resulting
    ``devices[].params`` shape). ``W``/``L`` (uppercase) match KLayout's own
    MOS convention so ``extract.py``'s ``_describe_devices`` reports them as
    ``w_um``/``l_um`` with no code change needed there.

    Shared by :func:`_build_mom_capacitor_extractor`'s own
    ``GenericDeviceExtractor.setup()`` (the layout-extraction side, which
    calls this once per fresh instance and reads the ids it needs back off
    the returned object's own terminal/parameter definitions) and
    :mod:`klayout_tools.netlist_capacitor_recovery`'s round-trip reader-side
    recognition (issue #1942) -- both sides register a structurally
    identical ``DeviceClass`` for the same ``name``, one call to build it, so
    they cannot silently drift apart.
    """
    import klayout.db as kdb

    device_class = kdb.DeviceClass()
    device_class.name = name
    terminal_a = kdb.DeviceTerminalDefinition("A", "Terminal A")
    device_class.add_terminal(terminal_a)
    terminal_b = kdb.DeviceTerminalDefinition("B", "Terminal B")
    device_class.add_terminal(terminal_b)
    device_class.equivalent_terminal_id(terminal_a.id(), terminal_b.id())
    param_w = kdb.DeviceParameterDefinition("W", "Width")
    device_class.add_parameter(param_w)
    param_l = kdb.DeviceParameterDefinition("L", "Length")
    device_class.add_parameter(param_l)
    return device_class


def _build_mom_capacitor_extractor(
    name: str, metal_count: int
) -> kdb.GenericDeviceExtractor:
    """Build a fresh ``kdb.GenericDeviceExtractor`` subclass instance that
    recognises one :class:`MomCapacitorDevice` entry (issue #1466) --
    IHP's ``cap_cmomi``/``cap_cmomf`` MoM (Metal-oxide-Metal) capacitors, and
    structurally any future device with the same "single marker, per-metal
    multi-port, position-split terminals, no computed value" shape.

    A **Python transcription of upstream's own ``CapMomExtractor``**
    (``custom_mom_extractor.lvs``, an ``RBA::GenericDeviceExtractor``
    subclass) -- kept as close to that source's structure and variable
    names as the Ruby/Python API difference allows, so the two can be
    diffed side by side. See :class:`~klayout_tools.decks.MomCapacitorDevice`
    for the device-recognition semantics this implements, and this
    function's own inline comments for the specific upstream lines each
    step mirrors.

    ``kdb.GenericDeviceExtractor`` is the Python-subclassable base KLayout's
    own built-in extractors (``DeviceExtractorCapacitor`` etc.) are written
    against internally -- the same "reimplement the C++ extension base
    class from script" mechanism this codebase already uses for
    ``kdb.NetlistSpiceWriterDelegate`` (``pdk_models.py``'s
    ``_ModelBindingSpiceWriterDelegate``) and ``kdb.GenericNetlistCompareLogger``
    (``lvs.py``'s ``_Logger``); this is the first such subclass for *device
    extraction* rather than netlist writing/comparison, because no built-in
    ``DeviceExtractor*`` class models this device's recognition shape (see
    ``MomCapacitorDevice``'s docstring for why).

    ``name`` becomes the extracted ``devices[].class`` value (and the
    device extractor/class name KLayout error/log messages cite).
    ``metal_count`` is the number of per-metal port layers to define --
    always ``len(deck.metals)`` for the owning :class:`MomCapacitorDevice`
    entry's deck, so a fresh instance's layer set lines up index-for-index
    with the caller's own ``metal_pins``/``metals`` regions (an empty
    ``kdb.Region`` at every index the entry's ``metal_pins`` left ``None``).

    A fresh instance is required per device *name* (not just per deck):
    ``kdb.GenericDeviceExtractor.setup()`` sets this extractor's own
    ``name``/registers its own device class once, so reusing one instance
    across ``cap_cmomi`` and ``cap_cmomf`` would register a second device
    class under the first one's name instead of two independent ones --
    mirroring why upstream's own ``cap_extraction.lvs`` constructs a fresh
    ``CapMomExtractor.new(...)`` per device too, despite both sharing the
    same Ruby class.
    """
    import klayout.db as kdb

    class _MomCapacitorExtractor(kdb.GenericDeviceExtractor):
        def __init__(self, extractor_name: str, num_metals: int) -> None:
            super().__init__()
            self._extractor_name = extractor_name
            self._num_metals = num_metals
            # Terminal/parameter ids are read back off their own definition
            # objects in `setup()`, once the device class is registered --
            # mirrors upstream's own `@reg_dev.terminal_id('mim_top')`
            # lookups rather than hard-coding the ids `add_terminal()`
            # happens to hand out in declaration order.
            self._terminal_a = 0
            self._terminal_b = 1

        def setup(self) -> None:
            # Mirrors upstream's private `define_layers`: `core` (the
            # marker), one per-metal port layer per declared metal level,
            # then `dev_mk` (the same marker again, kept as its own layer
            # for 1:1 parity with upstream's `core`/`dev_mk` split -- see
            # `custom_mom_extractor.lvs`'s own `define_layers`).
            self.name = self._extractor_name
            self.define_layer("core", f"{self._extractor_name} recognition marker")
            for metal_number in range(1, self._num_metals + 1):
                self.define_layer(f"m{metal_number}p", f"Metal{metal_number} pin ports")
            self.define_layer("dev_mk", "Device marker")

            # `DeviceCustomMIM`'s shape (`custom_mim_extractor.lvs`) --
            # built by `mom_capacitor_device_class` (shared with the
            # round-trip reader-side recognition `netlist_capacitor_
            # recovery.py` registers for the same name, issue #1942) so
            # both sides agree on the exact same terminal/parameter shape.
            # Terminal/parameter ids are read back off the returned
            # object's own definitions rather than assumed, mirroring
            # `mom_capacitor_device_class`'s own docstring note on why
            # (`add_terminal()`/`add_parameter()` write the id onto the
            # *argument* object, not the call's own return value).
            device_class = mom_capacitor_device_class(self._extractor_name)
            terminal_a, terminal_b = device_class.terminal_definitions()
            self._terminal_a = terminal_a.id()
            self._terminal_b = terminal_b.id()
            param_w, param_l = device_class.parameter_definitions()
            self._param_w = param_w.id()
            self._param_l = param_l.id()
            self.register_device_class(device_class)

        def get_connectivity(
            self, layout: kdb.Layout, layers: list[int]
        ) -> kdb.Connectivity:
            # Mirrors upstream's own `get_connectivity`: the marker
            # self-merges, and every per-metal port layer is scoped to
            # shapes touching the (duplicate) device-marker layer -- purely
            # an *internal* clustering connectivity for this extractor's own
            # marker-to-ports grouping, entirely separate from (and never
            # wired into) the outer `LayoutToNetlist` connectivity graph the
            # caller builds with its own `l2n.connect()` calls.
            core = layers[0]
            metal_port_layers = layers[1 : 1 + self._num_metals]
            dev_mk = layers[-1]
            conn = kdb.Connectivity()
            conn.connect(core, core)
            conn.connect(core, dev_mk)
            for metal_port_layer in metal_port_layers:
                conn.connect(metal_port_layer, dev_mk)
            return conn

        def extract_devices(self, layer_geometry: list[kdb.Region]) -> None:
            # Mirrors upstream's own `extract_devices` body closely -- see
            # its inline comments (transcribed into this function's own
            # docstring) for the full rationale of each step.
            core = layer_geometry[0]
            metal_ports = {
                metal_index + 1: layer_geometry[1 + metal_index]
                for metal_index in range(self._num_metals)
            }
            dev_mk = layer_geometry[-1]

            for component in dev_mk.merged().each():
                ports: list[tuple[Any, int]] = []
                for metal_index, port_region in metal_ports.items():
                    for polygon in port_region.each():
                        ports.append((polygon, metal_index))

                if len(ports) != 2:
                    # A well-formed device places exactly two `MkPin`s under
                    # its marker (upstream's own guard) -- skip anything
                    # else rather than guessing which two of N ports belong
                    # together. The usual real-world cause is two device
                    # markers close enough to touch and merge into one
                    # cluster, which drops BOTH devices, not just the
                    # malformed one.
                    self.error(
                        f"{self._extractor_name}: expected exactly 2 port "
                        f"regions under its recognition marker, found "
                        f"{len(ports)}. The device is not extracted. Check "
                        "for markers of adjacent devices touching or "
                        "overlapping.",
                        component,
                    )
                    continue

                device = self.create_device()

                # `l` -> marker bounding-box WIDTH (X extent), `w` -> HEIGHT
                # (Y extent) -- upstream's own axis mapping, transcribed
                # verbatim (`custom_mom_extractor.lvs`'s "Parameter/axis
                # mapping" comment).
                bbox = core.merged().bbox()
                device.set_parameter(self._param_l, bbox.width() * self.dbu())
                device.set_parameter(self._param_w, bbox.height() * self.dbu())

                # Deterministic pick of two ports (by x, then y, then metal
                # index) -- which of the two ends up on terminal A is
                # arbitrary, which is why the two terminals are declared
                # equivalent above; what matters is that each terminal is
                # defined on the layer that carries its own metal net, so
                # two ports stacked on adjacent metals (the `same`-feed
                # PCell configuration) stay on separate nets rather than
                # collapsing onto one.
                ports.sort(
                    key=lambda entry: (
                        entry[0].bbox().center().x,
                        entry[0].bbox().center().y,
                        entry[1],
                    )
                )
                a_polygon, a_metal_index = ports[0]
                b_polygon, b_metal_index = ports[-1]

                self.define_terminal(device, self._terminal_a, a_metal_index, a_polygon)
                self.define_terminal(device, self._terminal_b, b_metal_index, b_polygon)

    return _MomCapacitorExtractor(name, metal_count)


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
    interconnect_markers: kdb.Region | None = None,
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

    ``interconnect_markers`` (issue #1425) is the union of every shape drawn
    on the deck's optional ``ExtractionDeck.poly_interconnect`` marker layer
    -- a caller-drawn annotation saying "this poly is intentional
    interconnect" (most commonly a poly underpass: a strip contacted to
    metal at each end, routing one net beneath another). A component
    overlapping it at all is skipped unconditionally, the same way a
    component touching ``gate_regions``/``resistor_bodies`` is skipped above
    -- it never reaches the contact-cluster count or the resistor-marker
    split, and so is never counted, flagged, or warned about. ``None`` (the
    default) skips no component on this basis, matching this function's
    behaviour before #1425.

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
    interconnect = (
        interconnect_markers if interconnect_markers is not None else kdb.Region()
    )
    unmarked = 0
    marked_unrecognised = 0
    unmodelled_poly: list[dict[str, Any]] = []
    for component in poly.merged().each():
        candidate = kdb.Region(component)
        if not candidate.interacting(gate_regions).is_empty():
            continue
        if not candidate.interacting(bodies).is_empty():
            continue
        if not candidate.interacting(interconnect).is_empty():
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
    abstract_cell_local_candidates: (
        dict[int, dict[str, list[tuple[kdb.Point, str]]]] | None
    ) = None,
    abstract_cell_global_net_ports: dict[int, int] | None = None,
    abstract_body_identity_cover: tuple[kdb.Region, kdb.Region] | None = None,
    mom_net: str | None = None,
    mom_background_permittivity: float = MOM_CROSSCHECK_BACKGROUND_PERMITTIVITY,
    def_net_names: bool = False,
    critical_nets: frozenset[str] | None = None,
    parasitics_nets: frozenset[str] | None = None,
    parasitics_top_cell_only: bool = False,
    def_pins: frozenset[str] | None = None,
    pin_source_cells: frozenset[str] | None = None,
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
    dict[int, list[dict[str, Any]]],
    dict[int, list[dict[str, Any]]],
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
    mom_crosscheck, net_label_positions, device_instance_paths)``.
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

    ``net_label_positions`` (issue #1540) is ``{net.cluster_id: [{"text",
    "x_um", "y_um"}, ...], ...}`` -- every drawn text-label shape (on any of
    ``deck``'s ``well_label``/``poly_label``/``metal_labels`` layers) that
    names each *surviving* net, keyed by that net's own ``cluster_id`` rather
    than its (possibly collided) ``expanded_name()`` -- see
    :func:`_net_label_positions`'s own docstring for why a name-keyed map
    cannot do this job. Computed here, alongside ``parasitic_nets``/
    ``mom_crosscheck`` above, while ``l2n`` is still alive; :func:`run_extract`
    folds it into ``nets[].label_positions_um``/``nets[].net_id``/
    ``nets[].pin_index`` (see :func:`_describe_nets`).

    ``device_instance_paths`` (issue #1666), keyed by ``Device.id()``, is the
    device-level counterpart to ``net_label_positions`` above: which
    GDS-level instance placement (cell + array index, one entry per nesting
    level) each device's recognition shape positionally falls inside, per
    :func:`_device_instance_paths`. Computed here (not in
    :func:`run_extract`) because it needs ``layout``/``top_cell`` at the
    exact point the final top circuit's devices are known, mirroring how
    ``net_label_positions`` needs ``l2n`` while still alive; unlike
    ``net_label_positions`` it does not depend on ``l2n`` itself, only on
    ``layout``'s already-static instance tree, so it is computed once, right
    before this function returns. :func:`run_extract` folds it into
    ``devices[].instance_path`` (see :func:`_describe_devices`).

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

    ``pin_source_cells`` (issue #1513): applied right after ``def_pins``'s
    own pass -- a set of cell names whose own drawn pin-name labels
    (anywhere in the hierarchy, at any depth) are resolved to their real
    extracted net by probing each label's own composed-frame position
    (:func:`_pin_source_cell_net_names`), demoting every promoted pin not
    reached this way. ``None`` skips this entirely. See
    :func:`run_extract`'s own docstring for the full rationale.

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

    ``abstract_body_identity_cover``/``abstract_cell_global_net_ports``
    (issue #1911) are likewise computed by the same caller from the
    *pre*-erasure geometry. The first is
    :func:`_abstract_cell_body_identity_cover`'s ``(nwell cover,
    substrate-isolation cover)`` pair, unioned back into the whole-layout
    **body-identity classification** split below (``nwell_body_cover`` /
    ``isolation_region``) -- never into the conductor ``nwell`` region -- so
    black-boxing a cell that draws a well cannot silently reclassify a tie
    drawn *outside* it from "well tie" to "substrate tie" and merge its net,
    via ``connect_global``, with every other substrate-tied net in the
    design. The second is :func:`_abstract_cell_global_net_ports`' per-cell
    count of ports that only ever resolve through that same global, passed
    through to :func:`_wire_abstract_cells` for its ``warnings[]`` entry.
    Both ``None`` (the default) restore this function's pre-#1911
    behaviour exactly.
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

    # A well crosses cell boundaries physically. Capture from the original
    # layout both its body-identity classification (#1911) and electrical
    # continuity (#2082): erasure must neither turn an outside well tie into
    # a substrate tie nor split an abutted row's body pins into false islands.
    # The cover contains the exact instance-transformed polygons, so real
    # gaps remain gaps. Active/poly and other device-recognition geometry
    # stay erased inside black boxes; restoring well geometry cannot restore
    # their transistor channels or internal signal ties.
    abstract_nwell_cover, abstract_isolation_cover = (
        abstract_body_identity_cover
        if abstract_body_identity_cover is not None
        else (kdb.Region(), kdb.Region())
    )
    nwell_body_cover = (
        nwell if abstract_nwell_cover.is_empty() else nwell + abstract_nwell_cover
    )
    # Wells conduct across abutting cell boundaries even when the devices
    # inside those cells are abstracted. Keep the captured, transformed well
    # geometry for connectivity and body-pin probing (#2082); active/poly
    # geometry remains erased, so the black boxes still contain no devices.
    nwell = nwell_body_cover

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
        # `nwell_body_cover`, not `nwell` (issue #1911): this is a
        # body-identity *classification* -- which doping side of the well a
        # tie strip sits on -- so it must read the well as *drawn*, not as
        # `--abstract-cells` erasure left it (see `nwell_body_cover`'s own
        # comment above).
        tap = (
            (tap_nplus_region & active & nwell_body_cover)
            | (tap_pplus_region & (active - nwell_body_cover))
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
    # Unioned with the `--abstract-cells` cover for the same reason
    # `nwell_body_cover` is (issue #1911): an isolation island is a
    # body-identity *classification* for every NMOS body and substrate tie
    # inside it, so erasing a black-boxed cell's isolation layer would fold
    # an isolated block's `<substrate_net>_iso<n>` identity back into the
    # deck-wide `substrate_net` global -- merging the two, and every net on
    # them, design-wide. Empty (and inert) unless `--abstract-cells` matched
    # a cell drawing on this layer.
    isolation_region = (
        _region(layout, top_cell, deck.substrate_isolation) + abstract_isolation_cover
    )
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
    # `poly_interconnect_markers` (issue #1425) is the caller-drawn "this
    # poly is intentional interconnect" annotation from the deck's optional
    # `ExtractionDeck.poly_interconnect` layer (most commonly a poly
    # underpass) -- a component overlapping it is excluded from the
    # heuristic entirely, the same way a component touching a recognised
    # gate or resistor body is above. `_region` returns an empty `Region`
    # when the deck declares no such layer (or the stream has no shapes on
    # it), matching this function's behaviour before #1425.
    poly_interconnect_markers = _region(layout, top_cell, deck.poly_interconnect)
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
        poly_interconnect_markers,
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
    # `register` return values are captured here (unlike the geometry-role
    # layers above where every caller reads back through `layer_index`/
    # `metal_index`) so `net_label_positions` below (issue #1540) can read
    # each net's own drawn-label shapes back via `l2n.texts_of_net()` while
    # `l2n` is still alive, the same "read back before `l2n` dies" pattern
    # `layer_index`/`metal_index` already follow for `polygons_of_net`.
    label_layer_index: list[int] = [
        l2n.register(well_label, "well_label"),
        l2n.register(poly_label, "poly_label"),
    ]
    for index, texts in enumerate(metal_labels):
        label_layer_index.append(l2n.register(texts, f"metal{index}_label"))

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
    # `nwell_body_cover`, not `nwell` (issue #1911): "which side of the well
    # is this tie on" is a body-identity classification, and getting it
    # wrong here is the single most damaging way `--abstract-cells` erasure
    # can leak, because the answer feeds `connect_global` -- see
    # `nwell_body_cover`'s own comment above for the full derivation.
    tap_substrate = tap - nwell_body_cover
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

    # MoM (Metal-oxide-Metal) capacitor device recognition (issue #1466):
    # each of the deck's optional `mom_capacitors` entries (see
    # `MomCapacitorDevice` in `decks/__init__.py`) is recognised by a single
    # marker layer covering the whole device footprint, containing exactly
    # two per-metal port shapes told apart by *position* rather than by
    # declared layer -- structurally distinct from the MiM `capacitors`
    # block above (two independently-drawn plate layers, capacitance
    # computed from their geometric overlap). `_build_mom_capacitor_extractor`
    # builds a fresh `kdb.GenericDeviceExtractor` subclass instance per entry
    # (a Python transcription of upstream's own `CapMomExtractor`) that
    # reports only `W`/`L` (the marker's own bounding-box dimensions) as
    # matched parameters -- no capacitance value, since the real device's
    # `C` is supplied by the SPICE/Verilog-A model, not computed by LVS.
    for mom_capacitor in deck.mom_capacitors:
        # Deck-authoring validation (mirrors the capacitor block's own
        # `top_plate_via`/`top_plate_via_metal` pairing check above):
        # checked unconditionally so a mistake in a deck module is caught
        # even on a MoM-cap-free layout, since `metal_pins` must line up
        # index-for-index with `deck.metals` for the per-metal connectivity
        # wiring below to attach each port to the *right* metal level.
        if len(mom_capacitor.metal_pins) != len(deck.metals):
            raise ExtractError(
                f"mom_capacitor '{mom_capacitor.name}': metal_pins has "
                f"{len(mom_capacitor.metal_pins)} entries, but this deck "
                f"declares {len(deck.metals)} metals -- metal_pins must have "
                "exactly one entry (or None) per deck.metals level"
            )

        marker_region = _region(layout, top_cell, mom_capacitor.marker)

        # Dummy-device suppression (issue #295, extended to capacitors above
        # and to MoM capacitors here): count and cut whole recognised marker
        # components covered by the deck's `dummy` marker, the same
        # "component fully covered is dropped, partially covered is a clean
        # geometric cut" rule every other device family in this loop
        # applies.
        if not dummy.is_empty():
            for component in marker_region.merged().each():
                if (kdb.Region(component) - dummy).is_empty():
                    dummy_devices_dropped += 1
            marker_region = marker_region - dummy

        if marker_region.is_empty():
            # No PDK MoM-cap marker drawn anywhere on this layout -- the
            # common case. Registering/extracting an empty device would be
            # a no-op anyway, but skipping it entirely keeps a MoM-cap-free
            # layout's extraction bit-for-bit what it was before this
            # feature existed, the same guard the MiM capacitor block above
            # applies.
            continue

        # Per-metal port regions, index-aligned with `deck.metals`/`metals`
        # (an empty `Region` at every index `metal_pins` left `None` -- the
        # extractor below simply never finds a port there).
        port_regions = [
            _region(layout, top_cell, pin) if pin is not None else kdb.Region()
            for pin in mom_capacitor.metal_pins
        ]

        l2n.register(marker_region, f"{mom_capacitor.name}_marker")
        layer_geometry = {"core": marker_region, "dev_mk": marker_region}
        # NOTE: this loop's own index variable is deliberately named
        # `metal_level` rather than `metal_index` -- the latter is already
        # bound, earlier in this same function, to the list of registered
        # `metals[]` layer indices `_detect_dead_metal`'s call below reads
        # back. A plain `for` loop's target leaks into the enclosing
        # function scope in Python (unlike a comprehension's own scope), so
        # reusing that name here would silently clobber it with a bare
        # `int` on the last iteration.
        for metal_level, port_region in enumerate(port_regions):
            layer_name = f"m{metal_level + 1}p"
            l2n.register(port_region, f"{mom_capacitor.name}_{layer_name}")
            layer_geometry[layer_name] = port_region
            # Per-metal port connectivity (mirrors upstream's own
            # `cap_cmomi_connections.lvs`/`cap_cmomf_connections.lvs`): tie
            # each port *only* to its own metal's routed conductor, never to
            # every metal at once -- a merged "all ports to all metals"
            # connect would bridge two ports the real device keeps
            # electrically independent (the `same`-feed stacked-pin PCell
            # configuration in particular).
            if not port_region.is_empty():
                l2n.connect(port_region, metals[metal_level])

        l2n.extract_devices(
            _build_mom_capacitor_extractor(mom_capacitor.name, len(deck.metals)),
            layer_geometry,
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
        # takes the first hit. Well geometry includes the abstracted cells'
        # preserved cover (#2082); it must still never outrank a LEF pin's
        # drawn metal access. Poly/tap inside black boxes remain erased.
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
            abstract_cell_global_net_ports,
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
    # every currently-promoted pin whose net does not carry a declared name
    # -- the net keeps its name (a human/testbench can still find it), it is
    # simply not exposed as a top-level pin `combine_devices` must treat as
    # un-foldable. Applied *after* the top_cell_pins_only pass, so it can
    # only further restrict the promoted set, never re-promote a net that
    # pass already kept internal. Reuses `_reconcile_top_pins` exactly as
    # top_cell_pins_only does, with a different demote-set.
    #
    # Issue #1687: matching is done against a promoted net's comma-joined
    # component-label set (`set(name.split(",")) & declared_pins`), not the
    # whole joined string -- exactly the pattern `def_pins`'s own
    # reconciliation below already uses, and for the identical reason.
    # KLayout joins every distinct text label found on one electrical net
    # into a single, comma-separated `Net.name` (see `spice_safe_net_name`'s
    # docstring), so a net formed by composing two independently-labelled
    # blocks (e.g. a library cell's own pin label plus a top-level wire's
    # label landing on the same pad) can end up named e.g. `en,en1` -- a
    # name no single declared string can ever equal, and one `--pins`
    # (itself a comma-separated list) cannot even spell as a single entry.
    # Matching on any component label instead makes a hand-declared pin list
    # robust to a *future* extra label landing on the same net, too.
    if declared_pins is not None:
        top_circuit = netlist.circuit_by_name(top_cell.name)
        promoted_names: set[str] = set()
        if top_circuit is not None:
            for pin in top_circuit.each_pin():
                pin_net = top_circuit.net_for_pin(pin.id())
                if pin_net is not None and pin_net.name:
                    promoted_names.add(pin_net.name)

        matched_declared_pins: set[str] = set()
        non_matching_declared_pins: set[str] = set()
        for name in promoted_names:
            hit = set(name.split(",")) & declared_pins
            if hit:
                matched_declared_pins |= hit
            else:
                non_matching_declared_pins.add(name)

        demoted_by_declared_pins = _reconcile_top_pins(
            netlist, top_cell.name, non_matching_declared_pins, demote=True
        )
        if demoted_by_declared_pins:
            joined = ", ".join(demoted_by_declared_pins)
            warnings.append(
                f"kept {len(demoted_by_declared_pins)} net(s) internal: not in "
                f"the declared pin set (--pins / layout.declared_pins) "
                f"({joined}) -- issue #514"
            )

        unmatched_declared_pins = sorted(declared_pins - matched_declared_pins)
        if unmatched_declared_pins:
            joined = ", ".join(unmatched_declared_pins)
            count = len(unmatched_declared_pins)
            plural = "s" if count != 1 else ""
            warnings.append(
                f"{count} declared pin name{plural} (--pins / "
                f"layout.declared_pins) matched no promoted net in the "
                f"layout: {joined}"
            )

        # Issue #2000: a declared name can also match *more than one*
        # promoted net -- two physically disconnected islands that happen to
        # carry the identical drawn label. Both stay pins (see
        # `_duplicated_declared_pin_names`'s own docstring for why demoting
        # either is unsafe), so `pin_count` can exceed `len(declared_pins)`
        # with no other signal short of diffing the written `.SUBCKT` port
        # list -- surface it explicitly instead.
        duplicated_declared_pins = _duplicated_declared_pin_names(
            netlist, top_cell.name, declared_pins
        )
        if duplicated_declared_pins:
            joined = ", ".join(duplicated_declared_pins)
            count = len(duplicated_declared_pins)
            plural = "s" if count != 1 else ""
            warnings.append(
                f"{count} declared pin name{plural} (--pins / "
                f"layout.declared_pins) each matched 2+ physically "
                f"disconnected nets in the layout: {joined} -- every "
                f"matching net is kept promoted (demoting one would risk "
                f"hiding a genuine split-net connectivity defect from a "
                f"downstream `klt lvs` reference netlist), so pin_count can "
                f"exceed the declared set's size -- issue #2000"
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

        # Issue #2000: same duplicate-match diagnostic as `declared_pins`'s
        # own pass above, for `--def-pins` -- see
        # `_duplicated_declared_pin_names`'s docstring.
        duplicated_def_pins = _duplicated_declared_pin_names(
            netlist, top_cell.name, def_pins
        )
        if duplicated_def_pins:
            joined = ", ".join(duplicated_def_pins)
            count = len(duplicated_def_pins)
            plural = "s" if count != 1 else ""
            warnings.append(
                f"{count} declared DEF PINS name{plural} (--def-pins) each "
                f"matched 2+ physically disconnected nets in the layout: "
                f"{joined} -- every matching net is kept promoted (demoting "
                f"one would risk hiding a genuine split-net connectivity "
                f"defect from a downstream `klt lvs` reference netlist), so "
                f"pin_count can exceed the declared set's size -- issue #2000"
            )

    # Issue #1513: `pin_source_cells`'s own probe-based declared-pin
    # reconciliation -- a *positional* counterpart to `declared_pins`/
    # `def_pins` above, for a `klt gen-compose`d assembly of several
    # pre-labelled macros with no governing top-level DEF of its own to
    # anchor `def_pins` on. Neither `--top-cell-pins` (a composition's own
    # hand-drawn interconnect labels necessarily live in an *instanced*
    # sub-cell, not literally in the new top cell) nor `declared_pins`/
    # `def_pins` (their string-based matching cannot tell "the net whose
    # only relevant joined component is this string" from "any net with
    # this string as one of several", once two independently-labelled
    # macros happen to share a generic pin-name spelling) can express this
    # cleanly -- see `_pin_source_cell_net_names`'s own docstring. Applied
    # after `declared_pins`'s and `def_pins`'s own passes (when given), so
    # it can only further restrict -- it never re-promotes a net either of
    # those already kept internal.
    if pin_source_cells is not None:
        top_circuit = netlist.circuit_by_name(top_cell.name)
        promoted_names = set()
        if top_circuit is not None:
            for pin in top_circuit.each_pin():
                pin_net = top_circuit.net_for_pin(pin.id())
                if pin_net is not None and pin_net.name:
                    promoted_names.add(pin_net.name)

        pin_source_promoted_names, pin_source_unresolved_labels = (
            _pin_source_cell_net_names(
                l2n, layout, top_cell, deck, poly, nwell, tap, metals, pin_source_cells
            )
        )

        non_matching_pin_source = promoted_names - pin_source_promoted_names
        demoted_by_pin_source = _reconcile_top_pins(
            netlist, top_cell.name, non_matching_pin_source, demote=True
        )
        if demoted_by_pin_source:
            joined = ", ".join(demoted_by_pin_source)
            warnings.append(
                f"kept {len(demoted_by_pin_source)} net(s) internal: no "
                f"drawn label inside a --pin-source-cells cell resolves to "
                f"this net ({joined}) -- issue #1513"
            )

        if pin_source_unresolved_labels:
            joined = ", ".join(pin_source_unresolved_labels[:10])
            more = len(pin_source_unresolved_labels) - 10
            count = len(pin_source_unresolved_labels)
            plural = "s" if count != 1 else ""
            warnings.append(
                f"{count} label{plural} drawn inside a --pin-source-cells "
                f"cell resolved to no drawn conductor at its own position: "
                f"{joined}" + (f", +{more} more" if more > 0 else "")
            )

        # A label in a declared cell that resolved to a real net, but that
        # net was already demoted internal by an earlier `--top-cell-pins`/
        # `--pins`/`--def-pins` pass, cannot be re-promoted here (this pass
        # only ever further restricts, same as every declared-pin mechanism
        # above) -- surfaced so a caller combining `--pin-source-cells` with
        # an earlier demoting flag can see why a net it expected to survive
        # did not.
        already_demoted_by_earlier_pass = sorted(
            pin_source_promoted_names - promoted_names
        )
        if already_demoted_by_earlier_pass:
            joined = ", ".join(already_demoted_by_earlier_pass)
            count = len(already_demoted_by_earlier_pass)
            plural = "s" if count != 1 else ""
            warnings.append(
                f"{count} net{plural} named by a --pin-source-cells label "
                f"{'was' if count == 1 else 'were'} already kept internal "
                f"by an earlier --top-cell-pins/--pins/--def-pins pass, "
                f"before --pin-source-cells ran: {joined} -- "
                "--pin-source-cells can only further restrict the promoted "
                "set, never re-promote a net an earlier pass already "
                "demoted"
            )

    # Issue #1385: the final, cause-agnostic check -- after every promotion
    # and demotion pass above (`make_top_level_pins()`, `--top-cell-pins`,
    # `--pins`/`declared_pins`, `--def-pins`, `--pin-source-cells`) has run,
    # does the top circuit have *any* top-level pin left at all? A zero-pin
    # circuit means `klt lvs`'s `NetlistComparer` has no net/device anchor to
    # seed correspondence against a reference netlist and will report a full
    # mismatch even when the two sides' device populations genuinely agree
    # -- and that failure mode gives no hint the root cause is upstream in
    # pin promotion, not device extraction. This subsumes (but does not
    # replace) the label-layer-specific warning above: it also catches an
    # otherwise-labelled layout that `--top-cell-pins`/`--pins`/`--def-pins`/
    # `--pin-source-cells` demoted down to nothing between them.
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

    # Hierarchical net names (issue #2145): a net whose name carries an
    # instance path joined with `.` -- what `klt place-and-route`'s DEF net
    # names (`--def-net-names`) and hierarchy-qualified drawn labels both
    # produce -- is renamed to the `_`-joined, SPICE-addressable spelling
    # *here*, after the purge (so a net that is about to be dropped is never
    # renamed) and before anything reads a net name: the `devices[]`/`nets[]`
    # report, the parasitics passes (whose synthesized leg/hub nets are named
    # off their parent), the SPEF writer, `klt lvs`, and the written netlist
    # all then see one spelling. Unlike the `,`/leading-`$` cases,
    # `NetlistSpiceWriter` applies no escape of its own for `.`, so this has
    # to change the real net's name -- see `_rewrite_dotted_net_names`.
    warnings.extend(_dotted_rename_warnings(_rewrite_dotted_net_names(netlist)))

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
            layout=layout,
            top_cell=top_cell,
            critical_nets=critical_nets,
            parasitics_nets=parasitics_nets,
            parasitics_top_cell_only=parasitics_top_cell_only,
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

    # Issue #1540: per-net drawn-label geometry, keyed by `net.cluster_id` --
    # read back from `l2n` here, alongside `parasitic_nets`/`mom_crosscheck`
    # above, for the identical reason: `texts_of_net` is a live
    # `LayoutToNetlist` API, unusable once this function returns. Read from
    # the *final* top circuit (after the purge and pin promotion/demotion
    # passes above have already run), so `net_id`/`pin_index` in the JSON
    # response line up with the exact net objects `nets[]`/the written
    # `.SUBCKT` actually carry. The top circuit can be `None` here -- the
    # legitimate "nothing extracted" case (no devices, no named/labelled
    # nets, no subcircuits -- see `run_extract`'s own docstring comment on
    # why `circuit` can be `None`) -- in which case there is nothing to map.
    final_circuit_for_labels = netlist.circuit_by_name(top_cell.name)
    net_label_positions = (
        _net_label_positions(
            l2n, final_circuit_for_labels, layout.dbu, label_layer_index
        )
        if final_circuit_for_labels is not None
        else {}
    )

    # Issue #1666: per-device GDS-level instance attribution, keyed by
    # `Device.id()` -- unlike `net_label_positions` this needs no live `l2n`
    # API (only `layout`'s own static instance tree plus each device's own
    # `trans`), so it is computed straight from the same final top circuit,
    # read back exactly as `run_extract`'s own `_describe_devices` call will.
    # `Device.id()` survives the `netlist.dup()` below unchanged (KLayout
    # preserves per-device ids across a netlist duplication), so this
    # mapping keys correctly against the devices `run_extract` later reads
    # from the duplicated netlist.
    device_instance_paths = (
        _device_instance_paths(top_cell, final_circuit_for_labels)
        if final_circuit_for_labels is not None
        else {}
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
        net_label_positions,
        device_instance_paths,
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

    **Caveat (issue #1497):** the "each simple per-device sum" claim above
    describes ``combine_devices()``'s *documented* algorithm, not an
    unconditional guarantee about every call's actual result. A reported
    observation (10/10 repeat calls against one real, large -- roughly
    1000-device/20-group -- capacitor-only extracted netlist) found the
    primary ``C`` parameter sometimes left at a single pre-combine
    instance's own value instead of the group's summed total, while the
    same group's secondary ``A``/``P`` parameters combined correctly, with
    no exception raised. Neither that report's own reduction attempt nor a
    follow-up investigation could force this from a from-scratch synthetic
    netlist built directly via the ``klayout.db`` device/circuit API at a
    comparable scale, so it remains unconfirmed against this module's own
    extraction path specifically. ``klt lvs``'s ``options.combine_devices``
    wrapper (``lvs.py``'s ``_correct_capacitor_combine_parameters``)
    defends against it unconditionally -- by checking and, if necessary,
    correcting ``C`` against a pre-combine sum-conservation invariant after
    every combine, independent of whether the underlying KLayout behavior
    can be reproduced on demand -- rather than by patching this deferred-
    application argument, which only concerns *when* ``perim_cap_f_um`` is
    applied, not whether KLayout's own combine correctly sums the resulting
    ``C``. See ``docs/cli/lvs.md``'s "`device.combine_parameter_corrected`"
    section for the full mitigation.
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


def _instance_path_for_point(
    cell: kdb.Cell, point_um: kdb.DPoint
) -> list[dict[str, Any]]:
    """Positionally resolve ``point_um`` (micrometres, ``cell``'s own
    coordinate frame) to the chain of GDS-level instance placements that
    contains it -- issue #1666.

    ``_extract_netlist``'s device-recognition ``Region``s are built by
    flattening ``cell.begin_shapes_rec()`` (see :func:`region` in
    ``_layout.py``), which discards every instance transform before
    extraction ever runs -- so the flat ``kdb.Circuit`` it produces has no
    surviving handle back to the instance a given device came from. This
    reconstructs that association *after the fact*, purely positionally,
    using ``kdb.Cell.begin_instances_rec_touching`` -- KLayout's own
    recursive instance query, backed by a native spatial index, over a
    degenerate (zero-size) query box at ``point_um``. Each iterator step is
    one candidate placement chain (``it.path()`` -- the ancestor chain down
    to, but not including, the matched instance's own immediate parent --
    plus ``it.current_inst_element()``, the matched instance itself); a
    genuinely non-overlapping design (the common case this issue's
    "repeated instance" scenario describes) yields exactly one, the full
    chain down to wherever the device's recognition geometry actually
    lives, however many levels deep. Ties (``point_um`` touching more than
    one candidate chain -- overlapping placements, or an intermediate
    ancestor level whose own bounding box trivially contains a descendant
    match too) are broken deterministically: the *deepest* chain first (the
    most specific -- an ancestor-only partial match is always shallower than
    its own descendant's full match), then smallest matched bounding-box
    area, then ``(cell name, ia, ib)`` per level.

    **Why not a naive ``each_inst()`` walk** (this function's first cut):
    testing every sibling instance/array element at each nesting level, once
    per device, is O(devices x instances-at-that-level) -- fine for a small
    fixture, but field-observed to stall for minutes against a real
    place-and-route corpus block (`tests/corpus/place_and_route/gcd.gds.gz`,
    4303 top-level std-cell placements x thousands of recognized devices).
    The native spatial index turns each lookup into an O(log N + matches)
    query instead (empirically: 2000 random-point queries against that same
    corpus resolve in about 0.1s total).

    Returns ``[{"cell": str, "array_index": [ia, ib] | None}, ...]``, one
    entry per level descended, outermost first -- ``array_index`` is the
    element's 0-based ``(ia, ib)`` position within its ``CellInstArray`` when
    the instance is a regular array (``kdb.Instance.is_regular_array()``),
    ``None`` for a plain single placement. An empty list means ``point_um``
    touches no instance under ``cell`` at all (the device's recognition
    shapes were drawn directly in ``cell`` itself, not inside any placed
    sub-cell) -- a legitimate, common result, not an error.
    """
    import klayout.db as kdb

    region = kdb.DBox(point_um.x, point_um.y, point_um.x, point_um.y)
    iterator = cell.begin_instances_rec_touching(region)
    best_key: tuple[int, float, tuple[tuple[str, int, int], ...]] | None = None
    best_path: list[dict[str, Any]] = []
    while not iterator.at_end():
        elements = list(iterator.path())
        elements.append(iterator.current_inst_element())
        key_parts = tuple(
            (element.inst().cell.name, element.ia(), element.ib())
            for element in elements
        )
        bbox_area = iterator.inst_cell().dbbox().transformed(iterator.dtrans()).area()
        key = (len(elements), -bbox_area, key_parts)
        if best_key is None or key > best_key:
            best_key = key
            best_path = [
                {
                    "cell": element.inst().cell.name,
                    "array_index": (
                        [element.ia(), element.ib()]
                        if element.inst().is_regular_array()
                        else None
                    ),
                }
                for element in elements
            ]
        iterator.next()
    return best_path


def _device_instance_paths(
    top_cell: kdb.Cell, circuit: kdb.Circuit
) -> dict[int, list[dict[str, Any]]]:
    """Per-device GDS-level instance attribution, keyed by ``Device.id()``
    -- issue #1666.

    ``device.trans`` is KLayout's own record of "the position of the device"
    (its recognition shape's center, in micrometres, in ``top_cell``'s flat
    coordinate frame -- the same frame every other micrometre-valued field
    this module reports, e.g. ``nets[].label_positions_um``, already uses).
    Resolved via :func:`_instance_path_for_point` (which queries directly in
    this same micrometre frame -- no dbu/layout needed here at all), so a
    repeated leaf-cell instance's devices can be told apart positionally,
    the device-level analogue of what ``nets[].label_positions_um``/
    ``pin_index`` (issue #1540) already does for nets.

    Keyed by ``device.id()`` rather than by device index/name so it survives
    an intervening ``kdb.Netlist.dup()`` unchanged (verified: ``dup()``
    preserves every device's ``id()``) -- the same "resolve by object id, not
    by position or name" convention ``net_id``/``pin_index`` already follow.
    A device with an empty resolved path (see
    :func:`_instance_path_for_point`'s docstring) is simply absent from the
    returned mapping.
    """
    import klayout.db as kdb

    result: dict[int, list[dict[str, Any]]] = {}
    for device in circuit.each_device():
        disp = device.trans.disp
        point_um = kdb.DPoint(disp.x, disp.y)
        path = _instance_path_for_point(top_cell, point_um)
        if path:
            result[device.id()] = path
    return result
