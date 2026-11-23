"""Shared extraction interfaces, constants and formatting rules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

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
#:
#: 4 (issue #2659): the top-level `file` field -- the layout stream this run
#: read and hashed into `provenance.input.content_hash` -- changed from a raw
#: (usually absolute) path string to the same `{path, scope}` shape, for the
#: same reason #1376 moved `pdk.root`: a committed `klt extract` envelope is
#: cited verbatim by a `klt signoff` manifest, and an absolute path pinned
#: that record to the producing host's own filesystem, so `klt signoff`'s
#: `input_verified` freshness gate (issue #2196) could re-verify such a
#: citation only there and reported drift from every other checkout even
#: with every byte of evidence committed beside it.
#:
#: Only `file` -- the *output*-artifact paths this response also carries
#: (`netlist_path`, `spef_path`, `abstracted_cells[].lef_path`) stay plain
#: strings, per docs/json-contract.md's "Path fields: envelope vs. plain
#: string" table and its #2073 disposition. The `{path, scope}` shape
#: carries no repo root of its own, so a `scope: "repo"` entry is only
#: resolvable against *one* root per envelope (here: the input layout's,
#: which is the repo a cited envelope is committed in). An output written
#: outside that repo would therefore degrade to `path: null` and leave a
#: caller with no machine-readable handle on the file it just asked `klt` to
#: write -- a real loss for an output path, and no gain at all for
#: `input_verified`, which reads only `file` (`signoff.py`'s
#: `_INPUT_ARTIFACT_FIELDS`). See `env_provenance.repo_relative_path`.
SCHEMA_VERSION = 4


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


def substrate_tie_written_ohms(requested_ohms: float) -> float:
    """Resistance (ohms) the substrate tie card is actually written with.

    ``--substrate-tie-ohms 0`` asks for a *hard* tie, but a zero-ohm ``R``
    card is not legal SPICE (ngspice errors or clamps it). A hard tie is
    therefore written as the :data:`_MIN_PARASITIC_R_OHM` floor (1 mohm --
    the value the issue #3089 measurements used for a hard-grounded
    substrate); every positive request is written verbatim.
    """
    return requested_ohms if requested_ohms > 0 else _MIN_PARASITIC_R_OHM


def substrate_tie_mode(requested_ohms: float) -> str:
    """``"hard"`` for a ``0`` request, ``"shunt"`` otherwise (issue #3089)."""
    return "hard" if requested_ohms == 0 else "shunt"


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


def _substrate_tie_header_line(requested_ohms: float) -> str:
    """The ``* - substrate tie: ...`` header line (issue #3089)."""
    written = substrate_tie_written_ohms(requested_ohms)
    if substrate_tie_mode(requested_ohms) == "hard":
        return (
            f"* - substrate tie: hard tie to ground ({written:g} ohm; "
            "SPICE has no zero-ohm resistor) -- the substrate node is "
            "DC-fixed at 0 V in transient analysis"
        )
    if requested_ohms >= SUBSTRATE_DC_TIE_RESISTANCE_OHM:
        consequence = (
            "the substrate is capacitively floating in transient analysis "
            "(DC reference only); see --substrate-tie-ohms"
        )
    else:
        consequence = (
            "the substrate is held near 0 V in transient analysis through "
            "this resistance"
        )
    return f"* - substrate tie: {written:g} ohm to ground -- {consequence}"


def _parasitic_model_header_comment(
    substrate_tie_ohms: float = SUBSTRATE_DC_TIE_RESISTANCE_OHM,
) -> str:
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
    lines.append(_substrate_tie_header_line(substrate_tie_ohms))
    return "\n".join(lines)


#: The one top-level field a ``klt extract`` request document must carry
#: (issue #1867). ``deck`` is deliberately *not* required here: an omitted
#: deck is already reported as an application error (exit 1, "argument --deck
#: is required") by ``cli/extract_cmd.py``'s own ``run`` rather than as a
#: request-shape error -- see docs/cli/extract.md's exit-code contract.
_REQUIRED_REQUEST_FIELDS = ("file",)


#: Contract identifier a ``klt extract`` request document may declare in its
#: optional ``schema`` field (issue #1867).
REQUEST_SCHEMA = "klt.extract.request/1"


def _build_metrics(
    result: Mapping[str, Any], parasitics_report: Mapping[str, Any] | None
) -> dict[str, int | float]:
    """Assemble the additive ``metrics`` block (issue #1848)."""
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
    return metrics


#: ngspice's own hierarchy separator (issue #2145). A node token containing
#: it is parsed as a *path expression* (``XBIAS.vb1`` = instance ``XBIAS``,
#: node ``vb1``) wherever a node reference is read, so a flat net whose name
#: carries an instance path -- what ``klt place-and-route``'s DEF net names
#: and hierarchy-qualified drawn labels both produce -- is unaddressable by
#: its own written name in ``v()``/``.meas``/``.ic``. Only *net/node* tokens
#: are rewritten: SPICE dot-commands (``.SUBCKT``/``.ENDS``/``.GLOBAL``) and
#: numeric literals (``L=0.28U``) are a different lexical class entirely and
#: are never routed through :func:`spice_safe_net_name`.
SPICE_HIERARCHY_SEPARATOR = "."


#: What :data:`SPICE_HIERARCHY_SEPARATOR` is rewritten to (issue #2145).
#: ``_`` is an ordinary identifier character in every SPICE dialect and is
#: the join several other extractors already use; ``/`` was rejected because
#: ngspice's expression parser reads it as division inside ``v()``/``B``
#: source expressions. Not injective on its own (``a.b`` and a pre-existing
#: ``a_b`` both spell ``a_b``) -- no dot-free-preserving rewrite can be --
#: so collisions are resolved per-netlist, not per-name, by ``extract.py``'s
#: ``_rewrite_dotted_net_names``.
SPICE_SAFE_HIERARCHY_JOIN = "_"


def spice_safe_net_name(name: str) -> str:
    """Rewrite a KLayout ``Net.expanded_name()`` string to the exact spelling
    the written SPICE file uses for that net's *node* references in its
    ``.SUBCKT``/instance lines (issue #696, issue #1162, issue #2145).

    Three independent rewrites. The first two mirror escaping
    ``NetlistSpiceWriter`` already applies when it writes a net as a node
    reference (as opposed to the raw form it keeps in its own leading
    ``* pin ...``/``* net ...`` comments); the third mirrors a rewrite this
    repo applies to the *real* ``kdb.Net`` name before handing the netlist
    to the writer (``extract.py``'s ``_rewrite_dotted_net_names``), because
    ``NetlistSpiceWriter`` has no escape of its own for that character:

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
    3. **Hierarchical names (issue #2145).** A net whose name came from a
       composed/routed cell's own hierarchy -- a ``klt place-and-route``
       DEF net name replayed by ``--def-net-names``, or a drawn label
       carrying an instance path -- spells that path with ``.``
       (``XBIAS.vb1``). ``.`` is ngspice's *own* hierarchy separator, so
       such a token is read as a path expression (instance ``XBIAS`` ->
       node ``vb1``) wherever a node reference is parsed: the node is
       unaddressable by its written name in ``v()``/``.meas``/``.ic``, and
       the same token means two different things depending on its position
       on the card. ``NetlistSpiceWriter`` does *not* escape it (confirmed
       against a live writer run: a net named ``XBIAS.vb1`` writes as
       ``XBIAS.vb1``), so ``extract.py``'s ``_rewrite_dotted_net_names``
       renames the real net to ``_`` instead before the netlist is written,
       and this function applies the same ``.`` -> ``_`` rewrite so a name
       that reaches a report *without* passing through that pass (a
       reference netlist's nets in ``klt lvs``, say) is still reported in
       the one SPICE-addressable spelling.

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
    neither a comma, nor a dot, nor a leading ``$``. Idempotent: its own
    output contains none of the three, so re-applying it changes nothing.
    """
    escaped = name.replace(",", "|").replace(
        SPICE_HIERARCHY_SEPARATOR, SPICE_SAFE_HIERARCHY_JOIN
    )
    if escaped.startswith("$"):
        escaped = "\\" + escaped
    return escaped


#: ``FEED`` parameter codes (issue #2445) -- the ``cap_cmomi`` PCell's
#: feed-structure variant carried through a KLayout ``DeviceParameterDefinition``
#: (a ``double``) as a small integer enum, mapped back to the upstream
#: ``.subckt``'s own ``none``/``same``/``double`` token only when a card is
#: written (:data:`MOM_FEED_TOKENS`, consumed by ``pdk_models.py``) and when
#: ``devices[].feed`` is reported. ``0`` is **not** a variant: it is the
#: class's own default and means "this side never measured/stated the feed"
#: (the same sentinel role ``0`` plays for ``MMIN``/``MMAX``, issue #2435),
#: so an unrecognised port layout is left off the card rather than guessed
#: at. The codes are deliberately *not* the ``.lib``'s own ``none/same/double
#: -> 0/1/2`` numbering, precisely so ``0`` stays free for "unmeasured".
MOM_FEED_UNMEASURED = 0


MOM_FEED_NONE = 1


MOM_FEED_SAME = 2


MOM_FEED_DOUBLE = 3


MOM_FEED_TOKENS: dict[int, str] = {
    MOM_FEED_NONE: "none",
    MOM_FEED_SAME: "same",
    MOM_FEED_DOUBLE: "double",
}


class ExtractError(Exception):
    """Raised when a layout cannot be extracted: bad file, unknown deck,
    unresolvable PDK, missing/ambiguous top cell, or an engine error.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.

    ``code`` / ``job_id`` / ``details`` are optional and set only by the
    ``--backend batch`` path (:mod:`klayout_tools.extract_batch`), exactly as
    :class:`klayout_tools.netlist.NetlistError` carries them: ``code`` becomes
    the error envelope's ``error.code``, ``details`` is merged into it.
    """

    def __init__(
        self,
        *args: object,
        code: str | None = None,
        job_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(*args)
        self.code = code
        self.job_id = job_id
        self.details = details


def instantiated_device_counts(circuit: Any) -> dict[str, int]:
    """Count devices by class, once per instance reachable from the selected circuit."""
    memo: dict[str, dict[str, int]] = {}

    def count(node: Any, trail: tuple[str, ...]) -> dict[str, int]:
        if node.name in memo:
            return memo[node.name]
        out: dict[str, int] = {}
        for device in node.each_device():
            name = device.device_class().name
            out[name] = out.get(name, 0) + 1
        for call in node.each_subcircuit():
            called = call.circuit_ref()
            if called is None or called.name in trail:
                continue
            for name, n in count(called, (*trail, called.name)).items():
                out[name] = out.get(name, 0) + n
        memo[node.name] = out
        return out

    return dict(sorted(count(circuit, (circuit.name,)).items()))
