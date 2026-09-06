"""Convert a PDK schematic flow's *simulation* SPICE (the subcircuit-call
form) into the *schematic-equivalent, plain-element* form ``klt lvs`` requires
on its reference side (issue #280).

**Device family coverage (issue #1130).** The original (#280) scope was
MOS-only. This module now also recognises resistor, capacitor, and bipolar
subcircuit calls, resolving every family through the *same* curated
``pdk_models.py`` table :func:`~klayout_tools.pdk_models.create_model_binding_delegate`
already maintains for the opposite (plain-element -> subckt-call) direction
(issue #341) -- via :func:`~klayout_tools.pdk_models.known_device_subckt_names`/
:func:`~klayout_tools.pdk_models.build_device_binding_map`, never a second,
independent device-name mapping. A resistor/capacitor call is carried onto a
plain-element ``R``/``C`` card the same way a MOS call is carried onto ``M``
-- its own subcircuit's length/width call-site parameters (``l``/``w`` for
sky130, ``r_length``/``r_width`` or ``c_length``/``c_width`` for gf180mcu,
see ``pdk_models.py``'s module docstring) converted to explicit
micrometre-suffixed ``L=``/``W=`` (resistor) or a derived ``A=``/``P=``
plate area/perimeter (capacitor, ``area = L*W``, ``perimeter = 2*(L+W)`` --
elementary geometry, not a PDK-specific coefficient, so no deck-object
dependency is introduced). A bipolar call carries no length/width-style
parameter at all (sky130's fixed-geometry ``pnp_05v5`` cells are selected
purely by subcircuit name -- see ``pdk_models.py``'s bipolar section) and is
therefore recognised by a positive subcircuit-name match alone, not a
carried-parameter heuristic; its only carried call-site parameter is an
optional ``mult``, mapped onto the plain-element ``Q`` card's ``NE``
(KLayout's own ``DeviceClassBJT3Transistor`` natively represents multiple
parallel emitters via ``NE`` -- unlike MOS/resistor's ``nf``/``m``/``mult``,
which the curated plain-element form cannot represent at all and rejects,
see below, ``mult`` on a bipolar call is carried, not rejected).
Resistor/capacitor still reject ``nf``/``m``/``mult`` > 1 exactly like MOS
(neither ``DeviceClassResistor`` nor ``DeviceClassCapacitor`` has an
``NE``-equivalent multiplicity parameter to carry it onto).

**The gap this closes.** ``klt lvs``'s reference netlist must use the
plain-element device form ``klt extract`` writes -- a bare element line whose
leading letter names the device class and whose parameters are geometric
literals::

    M1 d g s b nfet L=0.5U W=1U

Real open-PDK schematic flows (xschem/ngspice against sky130 or gf180mcu) do
not emit that. Because both PDKs ship their primitive MOS device as a
``.subckt``, those flows emit the *simulation* form -- a subcircuit call::

    XM1 d g s b nfet_03v3 L=0.5u W=1u nf=1 ad='...' as='...' m=1

Handed to KLayout's ``NetlistSpiceReader`` as-is, each ``X`` card reads as an
instance of an *undefined* subcircuit; the circuit collapses toward one merged
net and the compare reports a confusing ``net.merged``/``topology`` cascade
that reads like a layout bug. This module does the small, PDK-parameterized,
correctness-sensitive transform back to the plain-element form, resolving
device names through the *same* curated table
(:mod:`klayout_tools.pdk_models`) that ``create_model_binding_delegate`` uses
in the opposite (plain-element -> subckt-call) direction -- never a second,
independent device-name mapping.

**Deliberately narrow, deliberately loud** (the issue's ``complex`` marker: a
wrong parameter/unit mapping here would not fail loudly -- it could produce a
plausible netlist that compares clean when it shouldn't). So:

- An ``X`` card is a candidate for conversion when its subcircuit name
  resolves against the curated table (any family, including bipolar, which
  carries no distinguishing parameter -- see "Device family coverage"
  above), or -- for MOS/resistor/capacitor only -- when it carries an
  ``l``/``w``-style geometry parameter even though the name did not resolve
  (the original #280 heuristic, now shared across those three families).
  Anything else (a genuine hierarchical subcircuit instance) passes through
  untouched.
- A device-like ``X`` card whose subcircuit name is **not** in the resolved
  device map is a hard error (:class:`NormalizeError`), never a silent
  pass-through -- an unrecognised device name is exactly the confusing-failure
  case this module exists to replace.
- ``L``/``W`` (MOS/resistor) or the ``A``/``P`` derived from them (capacitor)
  are carried, converted to explicit micrometre-suffixed literals (``0.5u``
  -> ``L=0.5U``; SI metres ``1.5e-6`` -> ``W=1.5U``). Every other parameter is
  dropped -- ``ad``/``as``/``pd``/``ps``/``nrd``/``nrs``/``sa``/``sb``/``sd``
  (parasitic-only, not carried by ``klt extract`` either) and any other model
  parameter -- matching the plain-element form's geometric-only scope.
  **A *bare* (unsuffixed, non-exponent) literal is not assumed to be SI
  metres unconditionally (issue #1492)**: it is resolved per ``deck``'s own
  :func:`~klayout_tools.pdk_models.geometry_style_for_family` convention --
  sky130's real schematic-flow netlists carry an ambient ``.option
  scale=1.0u`` and write already-micrometre bare literals (``L=0.15`` means
  0.15 um, matching a real fetched sky130A SRAM netlist -- see
  ``pdk_models.py``'s module docstring), while gf180mcu/sg13g2/sg13cmos5l
  ship no such ambient scale and a bare literal there is genuinely SI metres,
  unchanged from before this issue. Without ``deck`` (no family known to
  resolve the convention), a bare literal is a hard error rather than a
  silent metres assumption -- previously, a unitless-and-scaled schematic
  export silently mis-scaled every device geometry by ``1e6`` with no
  diagnostic (see :func:`_parse_um`).
- ``nf``/``m``/``mult`` > 1 (a multi-finger / multiplied device the curated
  plain-element resistor/capacitor forms cannot represent) is **rejected**
  with a specific error naming the device and value, never silently
  dropped or misinterpreted. Bipolar's ``mult`` is the one exception --
  carried onto ``NE``, not rejected (see "Device family coverage" above).
  **MOS's own ``nf`` is the other exception (issue #1487)**: a MOS ``nf>1``
  call is *expanded*, not rejected -- into ``nf`` parallel unit-width plain
  ``M`` cards (deterministic ``<instance>_f0``, ``<instance>_f1``, ...
  naming), the same shape a real drawn multi-finger layout extracts as
  (see :func:`_expand_mos_fingers`). MOS's ``m``/``mult`` (a *whole-device*
  replication count, a different real-world knob from finger-folding --
  gf180mcu spells it ``m``, sky130 ``mult``, never both on the same call)
  is still rejected exactly like resistor/capacitor's -- expanding it would
  need the same per-finger-width verification ``nf`` just received, and
  nothing here has done that yet.

Text-level (not KLayout-object-level) on purpose: the input is *not* readable
by ``NetlistSpiceReader`` in the first place (that is the whole problem), so
there is no netlist object to rewrite -- the transform operates on the SPICE
source and hands a plain-element source back to the reader.
"""

from __future__ import annotations

import re

from .pdk_models import (
    GEOMETRY_STYLE_BARE_UM,
    DeviceLookup,
    ModelBindingError,
    _format_um,
    _format_um2,
    build_device_binding_map,
    geometry_style_for_family,
    known_device_subckt_names,
)

#: Subcircuit-call parameters carried onto the plain-element ``M`` card
#: (MOS only -- also doubles as the "does this X card look like a MOS
#: device" detection gate, issue #280's original discipline, unchanged).
_CARRIED_PARAMS = ("l", "w")

#: The superset of every geometry-carrying family's own length/width
#: call-site parameter spellings (issue #1130) -- used only to decide
#: whether an ``X`` card whose subcircuit name does *not* resolve should be
#: a hard error (looks like a device call gone wrong) or a silent
#: passthrough (a genuine hierarchical subcircuit instance). Deliberately
#: excludes bipolar's ``mult`` -- unlike a geometry parameter, ``mult`` is a
#: plausible parameter name for an ordinary (non-device) parameterized
#: subcircuit too, so it is not a safe passthrough-vs-error signal on its
#: own; bipolar is recognised by a positive subcircuit-name match only (see
#: the module docstring).
_DEVICE_LIKE_PARAMS = _CARRIED_PARAMS + ("r_length", "r_width", "c_length", "c_width")

#: Parameters that select a multi-finger / multiplied device the curated
#: plain-element decks cannot represent -- rejected (not dropped) when > 1.
#: Shared by resistor/capacitor, and by MOS's own ``m``/``mult`` (issue
#: #1130, ``mf`` is sky130's own capacitor multiplier spelling, e.g.
#: ``sky130_fd_pr__cap_mim_m3_1 c0 c1 w=1 l=1 mf=1`` -- see
#: ``pdk_models.py``'s module docstring); bipolar's ``mult`` is handled
#: separately (carried onto ``NE``, see :func:`_convert_bipolar_card`).
#: MOS's own ``nf`` is *not* in this tuple -- issue #1487 expands it
#: instead of rejecting it (see :func:`_resolve_finger_count` and
#: :func:`_expand_mos_fingers`); resistor/capacitor calls still route their
#: own ``nf`` through this same rejection tuple (:func:`_convert_geometry_card`/
#: :func:`_convert_capacitor_card` call :func:`_reject_multiplicity` with no
#: ``exclude``), so a resistor/capacitor ``nf>1`` is still a hard error.
_MULTIPLICITY_PARAMS = ("nf", "m", "mult", "mf")

#: The multiplicity parameter :func:`_reject_multiplicity` never rejects for
#: a MOS call -- :func:`_convert_mos_card` excludes it because
#: :func:`_resolve_finger_count` handles it instead (issue #1487).
_MOS_EXCLUDED_MULTIPLICITY_PARAMS = ("nf",)

#: Terminal count of the curated capacitor/bipolar primitives, used the same
#: way :data:`_MOS_TERMINALS` is for MOS -- a hard, named error on a
#: mismatched node count rather than a confusing downstream failure. Not
#: validated for resistor: a real curated resistor subcircuit is legitimately
#: either 2-terminal (``r0 r1``) or 3-terminal (bulk-tied ``r0 r1 b`` --
#: sky130's ``res_high_po``/``res_xhigh_po``, see ``pdk_models.py``'s module
#: docstring), and which applies is not encoded in this module's
#: deck-object-free static tables.
_CAPACITOR_TERMINALS = 2
_BIPOLAR_TERMINALS = 3

#: SPICE engineering-notation multiplier suffixes, longest-match first so
#: ``meg`` is not shadowed by ``m``. The base unit for a MOS ``W``/``L`` is
#: metres, so each value is normalised to metres and then to micrometres.
_SPICE_SUFFIXES: tuple[tuple[str, float], ...] = (
    ("meg", 1e6),
    ("t", 1e12),
    ("g", 1e9),
    ("k", 1e3),
    ("m", 1e-3),
    ("u", 1e-6),
    ("n", 1e-9),
    ("p", 1e-12),
    ("f", 1e-15),
    ("a", 1e-18),
)

_NUMBER_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)$")

#: Terminal count of the curated MOS primitive (``d g s b``).
_MOS_TERMINALS = 4


class NormalizeError(Exception):
    """Raised when a subckt-call reference netlist cannot be converted
    correctly and unambiguously to the plain-element form: an unrecognised
    device subcircuit name, a non-numeric ``L``/``W`` value, an unexpected
    terminal count, or an ``nf``/``m`` > 1 the plain-element form cannot
    represent. Always names the offending device -- never a silent
    pass-through of a case that would degrade the compare downstream.
    """


def _tokenize(text: str) -> list[str]:
    """Whitespace-split ``text`` into SPICE tokens, treating quoted strings
    (``'...'``/``"..."``) and bracketed expressions (``{...}``/``(...)``) as
    single tokens and stopping at an unquoted ``$`` inline comment.

    A parasitic parameter like ``ad='int((nf+1)/2) * w/nf * 0.18u'`` carries
    spaces *inside* an expression -- a naive split would shatter it and
    misidentify the fragments as extra nodes, so quote/bracket awareness is
    load-bearing for correctness here, not a nicety.
    """
    tokens: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None

    for char in text:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            current.append(char)
            continue
        if char in "{(":
            depth += 1
            current.append(char)
            continue
        if char in "})":
            depth = max(0, depth - 1)
            current.append(char)
            continue
        if char == "$" and depth == 0:
            break  # rest of the line is a SPICE inline comment
        if char.isspace() and depth == 0:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)

    if current:
        tokens.append("".join(current))
    return tokens


def _merge_continuations(lines: list[str]) -> list[str]:
    """Join SPICE ``+`` continuation lines into their logical parent line.

    Comment (``*``) and blank lines are preserved as their own entries so
    they pass through the transform verbatim.
    """
    logical: list[str] = []
    for raw in lines:
        if raw.lstrip().startswith("+") and logical:
            logical[-1] = f"{logical[-1]} {raw.lstrip()[1:].strip()}"
        else:
            logical.append(raw)
    return logical


def _parse_um(
    raw_value: str,
    *,
    device: str,
    param: str,
    geometry_style: str | None = None,
) -> float:
    """Parse a SPICE ``L``/``W`` literal (``0.5u``, ``1.5e-6``, ``500n``,
    ``0.15``) to micrometres, raising :class:`NormalizeError` for anything
    that is not a plain number (e.g. a ``'...'`` expression the plain-element
    form cannot carry) or for a *bare* (unsuffixed, non-exponent) literal
    whose unit cannot be resolved (see below).

    An explicit engineering suffix (``u``/``n``/...) or exponent (``1.5e-6``)
    is unambiguous SPICE SI metres per the standard, and is parsed the same
    way regardless of ``geometry_style`` (issue #1492) -- this is the path
    the module's docstring's "explicit unit suffix... converts correctly"
    claim covers, and it is unchanged by this function.
    ``geometry_style`` -- normally
    :func:`~klayout_tools.pdk_models.geometry_style_for_family` resolved from
    the caller's ``deck`` -- only disambiguates a genuinely *bare* literal
    (no suffix, no exponent), which is otherwise ambiguous: it might be SI
    metres (the SPICE default), or it might already be micrometres under a
    vendor deck's ambient ``.option scale=1.0u`` (confirmed for sky130, see
    :data:`~klayout_tools.pdk_models._GEOMETRY_STYLE_BY_FAMILY`):

    - :data:`~klayout_tools.pdk_models.GEOMETRY_STYLE_BARE_UM` (sky130): the
      bare mantissa *is* the micrometre value already -- no ``1e6`` scaling.
    - :data:`~klayout_tools.pdk_models.GEOMETRY_STYLE_UNIT_SUFFIX` (every
      other curated family -- gf180mcu/sg13g2/sg13cmos5l ship no ambient
      ``.option scale`` and declare raw-metre subcircuit defaults): unchanged
      from before this issue -- a bare number is SI metres.
    - ``None`` (no ``deck`` given, so no family is known to resolve the
      convention): raises :class:`NormalizeError` naming the ambiguity,
      rather than silently assuming metres -- a wrong silent assumption here
      previously produced a plausible-looking but ~1e6x-mis-scaled netlist
      with no diagnostic (issue #1492).
    """
    match = _NUMBER_RE.match(raw_value.strip())
    if match is None:
        raise NormalizeError(
            f"device '{device}': parameter '{param.upper()}' value "
            f"'{raw_value}' is not a plain numeric literal -- the "
            "plain-element form cannot carry an expression; write an explicit "
            "geometric value (e.g. L=0.5u)"
        )
    mantissa_text = match.group(1)
    mantissa = float(mantissa_text)
    raw_suffix = match.group(2)
    # An exponent (`1.5e-7`) is captured into the mantissa group by
    # `_NUMBER_RE`, not the suffix group -- so an empty `raw_suffix` alone
    # does not mean "bare"; it must also lack an exponent marker to be a
    # genuinely ambiguous bare literal (issue #1492).
    has_exponent = "e" in mantissa_text.lower()
    if not raw_suffix and not has_exponent:
        if geometry_style is None:
            raise NormalizeError(
                f"device '{device}': parameter '{param.upper()}' value "
                f"'{raw_value}' has no unit suffix or exponent, so it is "
                "ambiguous whether it is SI metres or already-micrometres "
                "under an ambient SPICE '.option scale' (e.g. sky130's "
                "'.option scale=1.0u' convention) -- pass reference.deck "
                '(e.g. "sky130") so the convention can be resolved, or '
                "write an explicit unit-suffixed or exponent literal "
                "(e.g. L=0.5u or L=0.15e-6) instead"
            )
        if geometry_style == GEOMETRY_STYLE_BARE_UM:
            return mantissa
    suffix = raw_suffix.lower()
    multiplier = 1.0
    for name, value in _SPICE_SUFFIXES:
        if suffix.startswith(name):
            multiplier = value
            break
    metres = mantissa * multiplier
    return metres * 1e6


def _split_params(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split an ``X`` card's tokens (after the instance name) into its
    leading positional tokens (nodes + subcircuit name) and its
    ``key=value`` parameter map (keys lower-cased).

    A parameter is any token containing ``=``; positional tokens never do.
    """
    positional: list[str] = []
    params: dict[str, str] = {}
    for token in tokens:
        if "=" in token:
            key, _, value = token.partition("=")
            params[key.strip().lower()] = value.strip()
        else:
            positional.append(token)
    return positional, params


def _convert_x_card(
    line: str,
    subckt_to_binding: dict[str, DeviceLookup] | None,
    device_map_names: frozenset[str] = frozenset(),
    geometry_style: str | None = None,
) -> str:
    """Convert one ``X`` subcircuit-call line to a plain-element ``M``/``R``/
    ``C``/``Q`` line, or return it unchanged if it is not a recognised device
    call (issue #1130 extends the original #280 MOS-only conversion to
    resistor, capacitor, and bipolar).

    ``subckt_to_binding`` maps a device subcircuit name to its curated
    :class:`~klayout_tools.pdk_models.DeviceLookup`. When ``None`` (no deck /
    no explicit map given), the binding is resolved per-name against the
    whole curated table.

    ``device_map_names`` is the subset of ``subckt_to_binding`` keys that
    came from a caller-supplied ``device_map`` override (as opposed to
    ``deck``'s curated table) -- see :func:`_build_subckt_map`'s docstring.
    It is only used to give a clearer error (issue #1163) when such an
    override does not actually describe a MOS-shaped device.

    ``geometry_style`` (issue #1492) is the caller's resolved
    ``deck``-derived :func:`~klayout_tools.pdk_models.geometry_style_for_family`
    result (``None`` when no ``deck`` was given) -- forwarded to
    :func:`_parse_um` so a *bare* (unsuffixed) ``L``/``W``-style literal is
    interpreted per the deck's own ``.option scale`` convention instead of
    always assuming SI metres.
    """
    tokens = _tokenize(line)
    if not tokens:
        return line

    name_token = tokens[0]
    rest = tokens[1:]
    positional, params = _split_params(rest)
    if not positional:
        return line

    subckt_name = positional[-1]
    nodes = positional[:-1]

    # A device call is recognised either by its subcircuit name resolving
    # against the curated table (covers every family, including bipolar,
    # which carries no geometry-style parameter at all -- see the module
    # docstring), or, when the name does not resolve, by carrying a
    # geometry-style parameter that *looks* like a device call gone wrong
    # (the original #280 MOS-only heuristic, now shared with
    # resistor/capacitor). Neither signal present means a genuine
    # hierarchical subcircuit instance -- pass it through untouched.
    if not _binding_known(subckt_name, subckt_to_binding) and not any(
        key in params for key in _DEVICE_LIKE_PARAMS
    ):
        return line

    lookup = _resolve_binding(subckt_name, subckt_to_binding)
    instance = _instance_name(name_token, lookup.kind)

    if lookup.kind == "mos":
        return _convert_mos_card(
            instance,
            nodes,
            subckt_name,
            lookup,
            params,
            from_device_map=subckt_name in device_map_names,
            geometry_style=geometry_style,
        )
    if lookup.kind == "resistor":
        return _convert_geometry_card(
            instance, nodes, subckt_name, lookup, params, geometry_style=geometry_style
        )
    if lookup.kind == "capacitor":
        return _convert_capacitor_card(
            instance, nodes, subckt_name, lookup, params, geometry_style=geometry_style
        )
    return _convert_bipolar_card(instance, nodes, subckt_name, lookup, params)


def _convert_mos_card(
    instance: str,
    nodes: list[str],
    subckt_name: str,
    lookup: DeviceLookup,
    params: dict[str, str],
    *,
    from_device_map: bool = False,
    geometry_style: str | None = None,
) -> str:
    """The original #280 MOS conversion (``d g s b`` -> plain ``M`` card),
    now (issue #1487) also expanding a folded ``nf>1`` call into ``nf``
    parallel unit-width plain ``M`` cards instead of rejecting it -- see
    :func:`_resolve_finger_count`/:func:`_expand_mos_fingers`. Behaviour for
    ``nf`` absent or ``== 1`` is unchanged from #280.

    ``from_device_map`` (issue #1163) marks a binding that came from a
    caller-supplied ``device_map`` override that resolved to ``kind: "mos"``
    -- either the original bare-string shape (still MOS-only, unchanged), or
    an object-form entry that explicitly named ``"mos"`` (issue #1271). A
    terminal-count mismatch on such a binding gets a clear, actionable error
    naming the fix (an object-form ``device_map`` entry naming the real
    ``kind``, or ``deck``) instead of the generic mismatch message, since a
    bare-string entry cannot express a non-MOS device at all.
    """
    if len(nodes) != _MOS_TERMINALS:
        if from_device_map:
            raise NormalizeError(
                f"device '{instance}' (subcircuit '{subckt_name}'): "
                f"device_map only supports MOS-shaped {_MOS_TERMINALS}-terminal "
                f"overrides today, but this subcircuit has {len(nodes)} "
                f"terminal(s) ({' '.join(nodes) or '<none>'}) -- pass an "
                'object-form device_map entry ({"kind": "resistor"|'
                '"capacitor"|"bipolar", "class": ...}) naming the real '
                'device kind, or a deck ("sky130"/"gf180mcu") instead if '
                "this device is one of that deck's curated non-MOS classes, "
                "or flatten it out of the netlist"
            )
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): expected "
            f"{_MOS_TERMINALS} terminals (d g s b), found {len(nodes)} "
            f"({' '.join(nodes) or '<none>'})"
        )

    finger_count = _resolve_finger_count(instance, subckt_name, params)
    _reject_multiplicity(
        instance,
        subckt_name,
        params,
        exclude=_MOS_EXCLUDED_MULTIPLICITY_PARAMS,
    )

    for required in ("l", "w"):
        if required not in params:
            raise NormalizeError(
                f"device '{instance}' (subcircuit '{subckt_name}'): missing "
                f"required parameter '{required.upper()}' -- a MOS device "
                "call must supply both L and W"
            )

    l_um = _parse_um(
        params["l"], device=instance, param="l", geometry_style=geometry_style
    )
    w_um = _parse_um(
        params["w"], device=instance, param="w", geometry_style=geometry_style
    )

    if finger_count == 1:
        return (
            f"{instance} {' '.join(nodes)} {lookup.device_class} "
            f"L={_format_um(l_um)} W={_format_um(w_um)}"
        )
    return _expand_mos_fingers(
        instance, nodes, lookup.device_class, l_um, w_um, finger_count
    )


def _resolve_finger_count(
    instance: str, subckt_name: str, params: dict[str, str]
) -> int:
    """Parse and validate a MOS call's ``nf`` (default ``1`` when absent),
    returning the number of physical gate fingers to expand into (issue
    #1487).

    Unlike ``m``/``mult`` (a *whole-device* replication count -- still
    rejected outright by :func:`_reject_multiplicity`), ``nf`` describes a
    single real device folded into ``nf`` physical gate fingers -- a shape
    the curated plain-element MOS class cannot represent directly, but
    *can* be represented faithfully as ``nf`` separate unit-width plain
    ``M`` cards (see :func:`_expand_mos_fingers`). ``nf`` must be a
    positive integer -- there is no way to fold a fractional or negative
    number of physical gate fingers, so a non-integer value is still a
    hard error (never silently rounded).
    """
    if "nf" not in params:
        return 1
    raw = params["nf"]
    try:
        value = float(raw)
    except ValueError as exc:
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): "
            f"parameter 'nf' value '{raw}' is not numeric"
        ) from exc
    if value <= 0 or value != int(value):
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): nf={raw} "
            "must be a positive integer to expand into that many parallel "
            "plain-element gate fingers"
        )
    return int(value)


def _expand_mos_fingers(
    instance: str,
    nodes: list[str],
    device_class: str,
    l_um: float,
    w_um: float,
    finger_count: int,
) -> str:
    """Expand one folded ``nf``-finger MOS call into ``finger_count``
    parallel unit-width plain-element ``M`` cards, one line per finger,
    joined by newlines (issue #1487).

    Deterministic ``<instance>_f0``, ``<instance>_f1``, ... naming (``f``
    for "finger", 0-indexed) -- so device identity is stable across runs,
    which is what keeps ``klt lvs``'s device-pairing/association from
    churning between repeated conversions of the same source netlist.

    ``w_um`` is the call site's own **total** (un-folded) device width --
    verified empirically against the installed sky130A ngspice model
    library while building this fix: a diode-connected
    ``sky130_fd_pr__nfet_01v8`` at ``l=1 w=8`` measures (nearly) the same
    drain current whether ``nf=1`` or ``nf=4`` (only ``ad``/``as``-derived
    parasitics shift slightly), while ``l=1 w=2 nf=4`` measures ~1/4 of
    that current -- confirming ``w`` is the *total* width shared across all
    ``nf`` fingers, not a per-finger value, matching the standard BSIM
    convention. Each expanded finger therefore gets ``w_um / finger_count``
    -- the same per-finger width a real drawn multi-finger layout extracts
    as (see ``tests/test_lvs.py``'s ``_MULTIFINGER_LAYOUT_SPICE`` fixture:
    two ``W=0.325U`` fingers, electrically equivalent to one ``W=0.65U``
    device). ``klt lvs``'s own ``options.combine_devices``
    (``kdb.Netlist.combine_devices()``) then reconciles these expanded
    reference-side fingers against the layout's own folded fingers exactly
    the way it already reconciles a genuinely hand-split reference pair.
    """
    w_finger_um = w_um / finger_count
    lines = [
        f"{instance}_f{i} {' '.join(nodes)} {device_class} "
        f"L={_format_um(l_um)} W={_format_um(w_finger_um)}"
        for i in range(finger_count)
    ]
    return "\n".join(lines)


def _convert_geometry_card(
    instance: str,
    nodes: list[str],
    subckt_name: str,
    lookup: DeviceLookup,
    params: dict[str, str],
    *,
    geometry_style: str | None = None,
) -> str:
    """Convert a resistor call to a plain-element ``R`` card.

    KLayout's native ``DeviceClassResistor`` requires a positional value
    token before the model name (``R<name> n1 n2 [n3] <value> <model>``, the
    standard SPICE resistor shape); this module has no PDK sheet-resistance
    data to compute that value from geometry (that lives in
    ``klayout_tools.decks``, deliberately not a dependency of this module --
    see the module docstring), so it writes a placeholder ``0`` there and
    carries the call's own length/width geometry onto ``L=``/``W=`` instead
    -- ``DeviceClassResistor`` natively accepts both (confirmed against the
    installed ``klayout.db`` module), the same way the MOS path carries
    ``L=``/``W=``. Geometry is carried only when the call actually supplies
    it (the subcircuit's own default otherwise applies); a real curated
    resistor subcircuit is legitimately 2- or 3-terminal (see
    :data:`_CAPACITOR_TERMINALS`'s docstring note), so the terminal count is
    not validated here -- nodes pass through positionally, unchanged.
    """
    _reject_multiplicity(instance, subckt_name, params)
    extra = _geometry_suffix(
        instance, subckt_name, lookup, params, geometry_style=geometry_style
    )
    return f"{instance} {' '.join(nodes)} 0 {lookup.device_class}{extra}"


def _convert_capacitor_card(
    instance: str,
    nodes: list[str],
    subckt_name: str,
    lookup: DeviceLookup,
    params: dict[str, str],
    *,
    geometry_style: str | None = None,
) -> str:
    """Convert a capacitor call to a plain-element ``C`` card.

    ``DeviceClassCapacitor`` has no ``L``/``W`` parameter at all (only ``C``
    -- capacitance -- plus secondary ``A``/``P``, plate area/perimeter,
    confirmed against the installed ``klayout.db`` module); this mirrors
    ``pdk_models.py``'s own *forward*-direction capacitor binding, which
    solves ``equivalent_rectangle_um`` to recover ``L``/``W`` from a
    device's measured ``A``/``P``. This is the inverse, elementary
    computation -- ``area = L * W``, ``perimeter = 2 * (L + W)`` -- not a
    PDK-specific coefficient, so it introduces no deck-object dependency.
    Same ``0``-placeholder-value convention as :func:`_convert_geometry_card`
    (this module has no farad-per-area coefficient to compute a real
    capacitance from either -- that also lives in ``klayout_tools.decks``).
    """
    if len(nodes) != _CAPACITOR_TERMINALS:
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): expected "
            f"{_CAPACITOR_TERMINALS} terminals (A B), found {len(nodes)} "
            f"({' '.join(nodes) or '<none>'})"
        )
    _reject_multiplicity(instance, subckt_name, params)

    has_length = lookup.length_param in params
    has_width = lookup.width_param in params
    extra = ""
    if has_length or has_width:
        _require_both(instance, subckt_name, lookup, has_length, has_width)
        l_um = _parse_um(
            params[lookup.length_param],
            device=instance,
            param=lookup.length_param,
            geometry_style=geometry_style,
        )
        w_um = _parse_um(
            params[lookup.width_param],
            device=instance,
            param=lookup.width_param,
            geometry_style=geometry_style,
        )
        area_um2 = l_um * w_um
        perimeter_um = 2.0 * (l_um + w_um)
        extra = f" A={_format_um2(area_um2)} P={_format_um(perimeter_um)}"

    return f"{instance} {' '.join(nodes)} 0 {lookup.device_class}{extra}"


def _convert_bipolar_card(
    instance: str,
    nodes: list[str],
    subckt_name: str,
    lookup: DeviceLookup,
    params: dict[str, str],
) -> str:
    """Convert a bipolar call to a plain-element ``Q`` card.

    ``DeviceClassBJT3Transistor``'s native card shape puts the model name
    directly after the three nodes (``Q<name> c b e <model> [key=value
    ...]``, confirmed against the installed ``klayout.db`` module) -- unlike
    ``R``/``C``, no positional value token is needed at all. The real
    sky130 ``pnp_05v5`` subcircuits carry no length/width-style geometry
    parameter (fixed-geometry cells selected by name, see the module
    docstring); their only real call-site parameter is an optional
    ``mult``, carried onto the plain-element card's ``NE`` (number of
    parallel emitters), which ``DeviceClassBJT3Transistor`` natively
    supports -- so, unlike MOS/resistor/capacitor, ``mult`` here is carried,
    not rejected.
    """
    if len(nodes) != _BIPOLAR_TERMINALS:
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): expected "
            f"{_BIPOLAR_TERMINALS} terminals (c b e), found {len(nodes)} "
            f"({' '.join(nodes) or '<none>'})"
        )

    extra = ""
    if "mult" in params:
        try:
            mult = float(params["mult"])
        except ValueError as exc:
            raise NormalizeError(
                f"device '{instance}' (subcircuit '{subckt_name}'): "
                f"parameter 'mult' value '{params['mult']}' is not numeric"
            ) from exc
        extra = f" NE={mult:g}"

    return f"{instance} {' '.join(nodes)} {lookup.device_class}{extra}"


def _geometry_suffix(
    instance: str,
    subckt_name: str,
    lookup: DeviceLookup,
    params: dict[str, str],
    *,
    geometry_style: str | None = None,
) -> str:
    """The ``" L=...U W=...U"`` suffix carried from ``lookup``'s own
    length/width call-site parameters, or ``""`` when the call supplies
    neither (the subcircuit's own default geometry then applies)."""
    has_length = lookup.length_param in params
    has_width = lookup.width_param in params
    if not (has_length or has_width):
        return ""
    _require_both(instance, subckt_name, lookup, has_length, has_width)
    l_um = _parse_um(
        params[lookup.length_param],
        device=instance,
        param=lookup.length_param,
        geometry_style=geometry_style,
    )
    w_um = _parse_um(
        params[lookup.width_param],
        device=instance,
        param=lookup.width_param,
        geometry_style=geometry_style,
    )
    return f" L={_format_um(l_um)} W={_format_um(w_um)}"


def _require_both(
    instance: str,
    subckt_name: str,
    lookup: DeviceLookup,
    has_length: bool,
    has_width: bool,
) -> None:
    if has_length and has_width:
        return
    raise NormalizeError(
        f"device '{instance}' (subcircuit '{subckt_name}'): both "
        f"'{(lookup.length_param or '').upper()}' and "
        f"'{(lookup.width_param or '').upper()}' must be given together"
    )


def _reject_multiplicity(
    instance: str,
    subckt_name: str,
    params: dict[str, str],
    *,
    exclude: tuple[str, ...] = (),
) -> None:
    """Reject an X card whose ``nf``/``m``/``mult`` describes more than one
    device folded into a single call -- shared by MOS, resistor, and
    capacitor (issue #1130 generalises #280's original MOS-only check;
    bipolar's ``mult`` is handled separately, see
    :func:`_convert_bipolar_card`).

    ``exclude`` (issue #1487) skips a named parameter entirely -- the MOS
    call site passes ``_MOS_EXCLUDED_MULTIPLICITY_PARAMS`` (``("nf",)``)
    because :func:`_convert_mos_card` already resolved and validated ``nf``
    itself via :func:`_resolve_finger_count` (expanded, not rejected);
    resistor/capacitor call sites pass no ``exclude``, so their own ``nf``
    is still rejected exactly as before.
    """
    for param in _MULTIPLICITY_PARAMS:
        if param in exclude:
            continue
        if param not in params:
            continue
        try:
            value = float(params[param])
        except ValueError as exc:
            raise NormalizeError(
                f"device '{instance}' (subcircuit '{subckt_name}'): "
                f"parameter '{param}' value '{params[param]}' is not numeric"
            ) from exc
        if value > 1:
            raise NormalizeError(
                f"device '{instance}' (subcircuit '{subckt_name}'): "
                f"{param}={params[param]} describes a multi-finger/multiplied "
                "device the curated plain-element form cannot represent; "
                "flatten it in the schematic netlist (one device per drawn "
                "gate) before comparing"
            )


def _binding_known(
    subckt_name: str, subckt_to_binding: dict[str, DeviceLookup] | None
) -> bool:
    """Whether ``subckt_name`` resolves in the applicable curated table,
    without raising -- the passthrough-vs-error decision in
    :func:`_convert_x_card`."""
    if subckt_to_binding is not None:
        return subckt_name in subckt_to_binding
    return subckt_name in known_device_subckt_names()


def _resolve_binding(
    subckt_name: str, subckt_to_binding: dict[str, DeviceLookup] | None
) -> DeviceLookup:
    if subckt_to_binding is not None:
        lookup = subckt_to_binding.get(subckt_name)
        if lookup is None:
            available = ", ".join(sorted(subckt_to_binding)) or "<none>"
            raise NormalizeError(
                f"subcircuit '{subckt_name}' is not a known device for the "
                f"requested deck (known: {available}); if it is a real device, "
                "pass reference.device_map to map it explicitly"
            )
        return lookup

    known = known_device_subckt_names()
    entry = known.get(subckt_name)
    if entry is None:
        available = ", ".join(sorted(known)) or "<none>"
        raise NormalizeError(
            f"subcircuit '{subckt_name}' is not a known curated PDK device "
            f"(known: {available}); pass reference.deck or "
            "reference.device_map to map it explicitly"
        )
    _deck_name, lookup = entry
    return lookup


def _instance_name(name_token: str, kind: str = "mos") -> str:
    """Turn a subckt-call instance token into a plain element name.

    Idiomatically a device instance is emitted as e.g. ``XM1`` -- the ``X``
    subcircuit-call letter plus the device's own natural element name -- so
    dropping the leading ``X`` recovers ``M1``. When the remainder does not
    already start with the target kind's element letter (e.g. ``X5`` for a
    MOS device), that letter is prepended (``M5``) so the result is always a
    valid plain-element line. ``kind`` selects the letter (issue #1130:
    ``"R"``/``"C"``/``"Q"`` for resistor/capacitor/bipolar, ``"M"`` -- the
    original #280 default -- for MOS).
    """
    letter = {"mos": "M", "resistor": "R", "capacitor": "C", "bipolar": "Q"}[kind]
    rest = name_token[1:]  # drop the leading X/x
    if not rest:
        return name_token  # malformed; leave as-is for the reader to reject
    if rest[0].upper() == letter:
        return rest
    return f"{letter}{rest}"


#: Every ``kind`` an object-valued ``device_map`` entry may name (issue
#: #1271) -- the same four families :class:`DeviceLookup` already supports.
_DEVICE_MAP_KINDS = ("mos", "resistor", "capacitor", "bipolar")


def _device_lookup_from_override(name: str, value: object) -> DeviceLookup:
    """Build one ``device_map`` entry's :class:`DeviceLookup` from its raw
    JSON-decoded value (issue #1271).

    Two shapes are accepted:

    - A bare string (the original #280 shape, e.g. ``"nfet"``) always means a
      4-terminal MOS ``l``/``w`` binding -- **unchanged** from before this
      issue, for full backward compatibility with every existing caller's
      ``device_map``.
    - An object (``{"kind": ..., "class": ..., "length_param": ...,
      "width_param": ...}``) opts into an explicit, non-MOS-only binding.
      ``kind`` (one of :data:`_DEVICE_MAP_KINDS`) and ``class`` (the
      plain-element device-class label, e.g. ``"res_generic_po"``) are
      required. ``length_param``/``width_param`` (the real subcircuit's own
      call-site geometry parameter spellings, e.g. gf180mcu's
      ``"r_length"``/``"r_width"``) default to ``"l"``/``"w"`` and are
      ignored for ``kind: "bipolar"`` (no geometry call-site parameter at
      all -- see ``pdk_models.py``'s bipolar section); for ``kind: "mos"``
      they are likewise ignored, since :func:`_convert_mos_card` always
      reads the literal ``l``/``w`` parameter keys regardless of the
      resolved binding's own ``length_param``/``width_param`` (matching
      every curated MOS binding, which also always carries ``"l"``/``"w"``).

    Raises :class:`NormalizeError` (never a bare ``KeyError``/``TypeError``)
    for a malformed object entry -- this feeds a sign-off tool, so a
    misspelled key must fail loudly rather than silently produce a wrong
    binding.
    """
    if isinstance(value, str):
        return DeviceLookup("mos", value, "l", "w")
    if not isinstance(value, dict):
        raise NormalizeError(
            f"device_map entry '{name}': value must be a device-class string "
            f"or an object with a 'kind', found {type(value).__name__}"
        )
    kind = value.get("kind")
    if kind not in _DEVICE_MAP_KINDS:
        raise NormalizeError(
            f"device_map entry '{name}': 'kind' must be one of "
            f"{', '.join(_DEVICE_MAP_KINDS)}, found {kind!r}"
        )
    device_class = value.get("class")
    if not isinstance(device_class, str) or not device_class:
        raise NormalizeError(
            f"device_map entry '{name}': object form requires a non-empty "
            "'class' (the plain-element device-class label)"
        )
    if kind in ("mos", "bipolar"):
        # mos: `_convert_mos_card` always reads literal `l`/`w`, so a custom
        # spelling here would be silently ignored -- not accepted, to avoid
        # that trap. bipolar: no geometry call-site parameter exists at all.
        return DeviceLookup(kind, device_class, "l", "w")
    length_param = value.get("length_param", "l")
    width_param = value.get("width_param", "w")
    if not isinstance(length_param, str) or not length_param:
        raise NormalizeError(
            f"device_map entry '{name}': 'length_param' must be a non-empty string"
        )
    if not isinstance(width_param, str) or not width_param:
        raise NormalizeError(
            f"device_map entry '{name}': 'width_param' must be a non-empty string"
        )
    return DeviceLookup(kind, device_class, length_param, width_param)


def _build_subckt_map(
    deck: str | None, device_map: dict[str, object] | None
) -> tuple[dict[str, DeviceLookup] | None, frozenset[str]]:
    """Resolve the ``<subckt-name> -> DeviceLookup`` map for a conversion
    request from an optional deck name and/or an optional explicit override
    map. Returns ``(map, device_map_names)``; ``map`` is ``None`` when
    neither ``deck`` nor ``device_map`` is given (per-name auto-resolution
    against the whole curated table).

    Each ``device_map`` (issue #280's caller-supplied override) entry is
    resolved via :func:`_device_lookup_from_override` -- a bare string is
    always a MOS ``l``/``w`` binding (unchanged); an object value opts into
    an explicit non-MOS (or MOS) binding (issue #1271, closing the gap #1163
    only worked around with a clearer error). ``device_map_names`` (issue
    #1163) is the subset of the returned map's keys that came from
    ``device_map`` itself (as opposed to ``deck``) -- used only so a
    *bare-string* (still MOS-only) ``device_map`` entry naming a non-MOS
    device fails with a clear, named error (:func:`_convert_mos_card`)
    rather than an opaque terminal-count mismatch indistinguishable from a
    genuinely malformed netlist.

    ``deck``'s contribution comes from
    :func:`~klayout_tools.pdk_models.build_device_binding_map`, which since
    issue #1464 also derives an assumed-identity binding for each
    resistor/capacitor class the named deck's own ``ExtractionDeck`` declares
    but whose family the curated tables do not cover for that deck (e.g.
    ``sg13cmos5l``'s ``rsil``/``rppd``/``rhigh``, ``sg13g2``'s ``cap_cmim``)
    -- so a deck that recognises a device for *extraction* can read that
    same device back here without a hand-written ``device_map``, wherever
    the declared class name is also the upstream subcircuit name.
    (``sg13g2``'s ``rfcmim`` is the case where it is not: IHP ships
    ``.subckt cap_rfcmim``, so that one still needs ``device_map``.)
    ``device_map`` is still applied *after* ``deck``
    (``dict.update``, not ``setdefault``), so an explicit override wins over
    a curated **and** a derived binding alike.
    """
    resolved: dict[str, DeviceLookup] = {}
    if deck is not None:
        try:
            resolved.update(build_device_binding_map(deck))
        except ModelBindingError as exc:
            raise NormalizeError(str(exc)) from exc
    device_map_names: frozenset[str] = (
        frozenset(device_map) if device_map else frozenset()
    )
    if device_map:
        resolved.update(
            {
                name: _device_lookup_from_override(name, value)
                for name, value in device_map.items()
            }
        )
    return resolved or None, device_map_names


def normalize_reference_netlist(
    text: str,
    *,
    deck: str | None = None,
    device_map: dict[str, object] | None = None,
) -> str:
    """Convert subckt-call-form SPICE ``text`` to the plain-element form.

    ``deck`` selects that registered deck's device map (``"sky130"``/
    ``"gf180mcu"``/``"sg13g2"``/``"sg13cmos5l"`` -- see
    :func:`_build_subckt_map` for what that map covers);
    ``device_map`` is an explicit ``<subckt-name> -> <override>`` override
    (merged on top of the deck's map), where ``<override>`` is either a bare
    device-class string (always a MOS ``l``/``w`` binding, unchanged since
    #280) or an object ``{"kind": ..., "class": ..., "length_param": ...,
    "width_param": ...}`` naming an explicit non-MOS (or MOS) binding (issue
    #1271 -- see :func:`_device_lookup_from_override`). With neither ``deck``
    nor ``device_map``, each device subcircuit name is auto-resolved against
    the whole curated table.

    Non-device lines (comments, ``.subckt``/``.ends``/``.model``, existing
    plain-element ``M`` cards, genuine hierarchical ``X`` instances) pass
    through unchanged, so a netlist that *mixes* plain-element and subckt-call
    device lines converts correctly. Raises :class:`NormalizeError` for any
    device card that cannot be converted correctly and unambiguously.

    A *bare* (unsuffixed, non-exponent) ``L``/``W``-style geometry literal
    (issue #1492) is resolved per ``deck``'s own
    :func:`~klayout_tools.pdk_models.geometry_style_for_family` convention --
    e.g. sky130's real ``.option scale=1.0u`` netlists write already-
    micrometre bare literals (``L=0.15`` means ``0.15`` um, not ``0.15`` m).
    Without ``deck``, no such convention is known, so a bare literal raises
    :class:`NormalizeError` instead of silently assuming SI metres (a wrong
    assumption there previously produced a plausible-looking but
    ~1e6x-mis-scaled netlist with no diagnostic pointing at units). An
    explicit unit suffix or exponent is unaffected either way -- see
    :func:`_parse_um`.
    """
    subckt_to_binding, device_map_names = _build_subckt_map(deck, device_map)
    geometry_style = geometry_style_for_family(deck) if deck is not None else None
    logical_lines = _merge_continuations(text.splitlines())

    out: list[str] = []
    for line in logical_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            out.append(line)
            continue
        first = stripped.split(None, 1)[0]
        if first and first[0] in "Xx":
            out.append(
                _convert_x_card(
                    line, subckt_to_binding, device_map_names, geometry_style
                )
            )
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def detect_subckt_call_devices(text: str) -> list[str]:
    """Return the curated PDK device subcircuit names an ``X`` card in ``text``
    instantiates *without* a matching ``.subckt`` definition in the same file.

    Used by ``klt lvs`` to turn the silent subckt-call degradation into a
    specific, actionable error when the caller did *not* opt into conversion:
    a non-empty result means the reference netlist is in the simulation
    (subckt-call) form, not the plain-element form ``klt lvs`` requires. A
    device subcircuit that *is* defined in the file is not reported -- the
    reader can at least read it as a subcircuit, a different (out-of-scope)
    case.

    Covers every curated device family (issue #1130: MOS, resistor,
    capacitor, bipolar), not just MOS -- a reference netlist whose only
    subckt-call devices are e.g. resistors is now detected the same way a
    MOS-only one always was.
    """
    known = known_device_subckt_names()
    defined: set[str] = set()
    used: list[str] = []
    seen: set[str] = set()

    for line in _merge_continuations(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        lowered = stripped.lower()
        if lowered.startswith(".subckt"):
            parts = stripped.split()
            if len(parts) >= 2:
                defined.add(parts[1])
            continue
        first = stripped.split(None, 1)[0]
        if not first or first[0] not in "Xx":
            continue
        tokens = _tokenize(line)
        positional, _params = _split_params(tokens[1:])
        if not positional:
            continue
        subckt_name = positional[-1]
        if subckt_name in known and subckt_name not in seen:
            used.append(subckt_name)
            seen.add(subckt_name)

    return [name for name in used if name not in defined]
