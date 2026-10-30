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
parallel emitters via ``NE`` -- unlike resistor's ``nf``/``m``/``mult``,
which the curated plain-element form cannot represent at all and rejects,
see below, ``mult`` on a bipolar call is carried, not rejected).
Resistor/capacitor reject ``nf``/``m``/``mult`` > 1 (MOS expands ``nf``/``m``
instead; neither ``DeviceClassResistor`` nor ``DeviceClassCapacitor`` has an
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
- resistor/capacitor ``nf``/``m``/``mult`` > 1 (a multi-finger / multiplied
  device the curated plain-element resistor/capacitor forms cannot
  represent) is **rejected**
  with a specific error naming the device and value, never silently
  dropped or misinterpreted. Bipolar's ``mult`` is the one exception --
  carried onto ``NE``, not rejected (see "Device family coverage" above).
  **MOS's own ``nf`` is the other exception (issue #1487)**: a MOS ``nf>1``
  call is *expanded*, not rejected -- into ``nf`` parallel unit-width plain
  ``M`` cards (deterministic ``<instance>_f0``, ``<instance>_f1``, ...
  naming), the same shape a real drawn multi-finger layout extracts as
  (see :func:`_expand_mos_fingers`). **MOS's instance multiplier ``m`` is
  expanded too (issue #3123)**: ``m=N`` becomes ``N`` parallel plain ``M``
  cards (``<instance>_m0`` ... ; ``nf*m`` cards named ``<instance>_m0_f0``
  ... when combined with ``nf``), total width ``m*W``, reconciled against a
  drawn N-unit layout by ``options.combine_devices``. MOS's ``mult`` is
  ignored (it must still be numeric): verified against the sky130 model
  subckt, ``mult`` only enters the mismatch-sigma expressions
  (``sqrt(l*w*mult)``) and is never forwarded to the device card, so only
  ``m`` changes the nominal device. ``m`` must be a positive integer.
- **A resistor/capacitor call supplying only one of its two geometry
  parameters is not automatically an error (issue #2459).** Some PDKs ship
  a *fixed-geometry device-flavour wrapper* -- its own separately-named
  ``.subckt`` that bakes one dimension (typically width) into the name
  itself, accepting only the other at the call site (sky130's
  ``sky130_fd_pr__res_high_po_2p85``, ``w=2.85`` fixed, ``l`` still a real
  call-site parameter). When :class:`~klayout_tools.pdk_models.DeviceLookup`
  carries a fixed constant for the omitted dimension -- curated (see
  ``pdk_models.py``'s ``_RESISTOR_FIXED_WIDTH_TABLE``) or from an explicit
  ``reference.device_map`` object entry's ``"length_um"``/``"width_um"`` --
  that constant fills the gap; a call-site value for the same dimension
  still wins when the netlist happens to supply one. Genuinely missing
  geometry (no call-site value **and** no fixed constant for the same
  dimension) is still the original hard error, now naming the
  ``device_map`` escape hatch instead of leaving a caller to invent a
  sentinel unused parameter name. See :func:`_resolve_geometry_dimension`.

Text-level (not KLayout-object-level) on purpose: the input is *not* readable
by ``NetlistSpiceReader`` in the first place (that is the whole problem), so
there is no netlist object to rewrite -- the transform operates on the SPICE
source and hands a plain-element source back to the reader.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from typing import Any, NamedTuple

from . import _spice_number as spice_number
from ._paths import _fold_spice_continuations
from .pdk_models import (
    GEOMETRY_STYLE_BARE_UM,
    DeviceLookup,
    ModelBindingError,
    _format_um,
    _format_um2,
    build_device_binding_map,
    build_subckt_to_class_map,
    flavoured_mos_class_name,
    geometry_style_for_family,
    known_device_subckt_names,
    mos_flavour_for_subckt,
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
#: :func:`_convert_capacitor_card` call :func:`_reject_multiplicity`), so a
#: resistor/capacitor ``nf>1`` is still a hard error. A capacitor's instance
#: multiplier ``m`` is the exception: it is excluded and expanded into ``m``
#: parallel plain ``C`` cards (issue #4318), so only its ``nf``/``mult``/``mf``
#: are still rejected.
_MULTIPLICITY_PARAMS = ("nf", "m", "mult", "mf")

#: The multiplicity parameters :func:`_reject_multiplicity` never rejects for
#: a MOS call -- :func:`_convert_mos_card` excludes them because
#: :func:`_resolve_finger_count` (``nf``, issue #1487),
#: :func:`_resolve_multiplier` (``m``, issue #3123) and
#: :func:`_check_mos_mult` (``mult``, mismatch-only) handle them instead.
_MOS_EXCLUDED_MULTIPLICITY_PARAMS = ("nf", "m", "mult")

#: The multiplicity parameter :func:`_reject_multiplicity` never rejects for a
#: capacitor call -- :func:`_convert_capacitor_card` expands ``m`` (issue #4318).
_CAPACITOR_EXCLUDED_MULTIPLICITY_PARAMS = ("m",)

#: Terminal count of the curated capacitor primitive, used the same way
#: :data:`_MOS_TERMINALS` is for MOS -- a hard, named error on a mismatched
#: node count rather than a confusing downstream failure. Not validated for
#: resistor: a real curated resistor subcircuit is legitimately either
#: 2-terminal (``r0 r1``) or 3-terminal (bulk-tied ``r0 r1 b`` -- sky130's
#: ``res_high_po``/``res_xhigh_po``, see ``pdk_models.py``'s module
#: docstring), and which applies is not encoded in this module's
#: deck-object-free static tables. Bipolar accepts the three-terminal
#: vertical-PNP shape *and*, since issue #2862, the four-terminal
#: (``c b e s``) substrate-terminal shape -- both are plain
#: ``DeviceClassBJT{3,4}Transistor`` cards -- so its accepted count is
#: spelled out in :func:`_convert_bipolar_card` rather than in a single
#: constant.
_CAPACITOR_TERMINALS = 2

#: SPICE's own ``PARAMS:`` keyword -- the optional separator between an ``X``
#: card's positional tokens (nodes + subcircuit name) and its ``key=value``
#: parameters (issue #2327). ``kdb.NetlistSpiceWriter`` emits it for every
#: custom (``GenericDeviceExtractor``-recognised) device class it round-trips
#: as an ``X`` card -- ``XD_$1 A B cap_cmomi PARAMS: W=1 L=2`` -- and
#: ``kdb.NetlistSpiceReader`` consumes it natively. It carries no ``=``, so
#: without this it would be mistaken for a positional token (and, being the
#: *last* one, for the subcircuit name itself). Matched case-insensitively,
#: like every other SPICE keyword this module reads.
_PARAMS_SEPARATOR = "params:"

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
    single tokens and stopping at an unquoted ``$`` inline comment (a ``$``
    that begins a token; mid-token it belongs to the name, e.g. ``X$1``).

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
        if char == "$" and depth == 0 and not current:
            # Rest of the line is a SPICE inline comment. Only at a token
            # start: KLayout names cards `X$1`/`M$1`, so a mid-token `$` is
            # part of the name (issue #2972).
            break
        if char.isspace() and depth == 0:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)

    if current:
        tokens.append("".join(current))
    return tokens


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
    match = spice_number.NUMBER_RE.match(raw_value.strip())
    if match is None:
        symbols = sorted(set(_IDENT_RE.findall(raw_value)) - {"e", "E"})
        hint = (
            f" (unresolved symbol(s) {', '.join(repr(n) for n in symbols)}: not "
            "defined by a .subckt header default or .param line in this "
            "netlist, or the expression uses unsupported syntax)"
            if symbols
            else ""
        )
        raise NormalizeError(
            f"device '{device}': parameter '{param.upper()}' value "
            f"'{raw_value}' is not a plain numeric literal{hint} -- the "
            "plain-element form cannot carry an expression; write an explicit "
            "geometric value (e.g. L=0.5u)"
        )
    mantissa_text = match.group(1)
    mantissa = float(mantissa_text)
    raw_suffix = match.group(2)
    # An exponent (`1.5e-7`) is captured into the mantissa group by
    # `spice_number.NUMBER_RE`, not the suffix group -- so an empty `raw_suffix` alone
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
    metres = mantissa * spice_number.suffix_scale(raw_suffix)
    return metres * 1e6


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUM_TOKEN_RE = re.compile(r"(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?[a-zA-Z]*")

#: ``{<subckt-name or "">: {<param>: <raw value>}}`` -- the ``.param`` lines
#: (scope ``""``) and ``.subckt`` header defaults found in one netlist.
ParamScopes = dict[str, dict[str, str]]


class _Unresolved(Exception):
    """An expression names a symbol it cannot resolve, or uses syntax outside
    the restricted evaluator; ``symbol`` is the offending name/token."""

    def __init__(self, symbol: str) -> None:
        super().__init__(symbol)
        self.symbol = symbol


def _collect_param_scopes(lines: list[str]) -> ParamScopes:
    """Gather ``.param`` assignments and ``.subckt`` header ``key=value``
    defaults, keyed by the enclosing subcircuit name (``""`` = top level)."""
    scopes: ParamScopes = {"": {}}
    current = ""
    for line in lines:
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith(".subckt"):
            tokens = _tokenize(stripped)
            if len(tokens) >= 2:
                current = tokens[1].lower()
                scopes.setdefault(current, {}).update(_split_params(tokens[2:])[1])
        elif lowered.startswith(".ends"):
            current = ""
        elif lowered.startswith(".param"):
            tokens = _tokenize(stripped)
            scopes.setdefault(current, {}).update(_split_params(tokens[1:])[1])
    return scopes


class _ExprEvaluator:
    """Restricted SPICE arithmetic: ``+ - * /``, parentheses, unary sign,
    engineering suffixes and identifiers looked up through ``lookup``. No
    function calls. ``scaled`` records whether any literal carried a
    suffix/exponent, so the caller can keep the result unambiguous."""

    def __init__(self, text: str, lookup: Callable[[str], tuple[float, bool]]) -> None:
        self.text = text
        self.pos = 0
        self.lookup = lookup
        self.scaled = False

    def parse(self) -> float:
        value = self._sum()
        self._skip()
        if self.pos != len(self.text):
            raise _Unresolved(self.text[self.pos :])
        return value

    def _skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def _peek(self) -> str:
        self._skip()
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _sum(self) -> float:
        value = self._product()
        while self._peek() in ("+", "-"):
            op = self.text[self.pos]
            self.pos += 1
            rhs = self._product()
            value = value + rhs if op == "+" else value - rhs
        return value

    def _product(self) -> float:
        value = self._unary()
        while self._peek() in ("*", "/"):
            op = self.text[self.pos]
            self.pos += 1
            rhs = self._unary()
            if op == "/" and rhs == 0:
                raise _Unresolved("/0")
            value = value * rhs if op == "*" else value / rhs
        return value

    def _unary(self) -> float:
        if self._peek() in ("+", "-"):
            sign = self.text[self.pos]
            self.pos += 1
            value = self._unary()
            return -value if sign == "-" else value
        return self._atom()

    def _atom(self) -> float:
        char = self._peek()
        if char == "(":
            self.pos += 1
            value = self._sum()
            if self._peek() != ")":
                raise _Unresolved("(")
            self.pos += 1
            return value
        number = _NUM_TOKEN_RE.match(self.text, self.pos)
        if number is not None:
            self.pos = number.end()
            match = spice_number.NUMBER_RE.match(number.group(0))
            if match is None:
                raise _Unresolved(number.group(0))
            suffix = match.group(2)
            if suffix or "e" in match.group(1).lower():
                self.scaled = True
            return float(match.group(1)) * spice_number.suffix_scale(suffix)
        ident = _IDENT_RE.match(self.text, self.pos)
        if ident is None:
            raise _Unresolved(self.text[self.pos : self.pos + 1] or "<end>")
        self.pos = ident.end()
        if self._peek() == "(":
            raise _Unresolved(f"{ident.group(0)}(...)")
        value, scaled = self.lookup(ident.group(0))
        self.scaled = self.scaled or scaled
        return value


def _resolve_value(
    raw: str,
    chain: list[dict[str, str]],
    active: tuple[str, ...] = (),
) -> tuple[float, bool]:
    """Evaluate ``raw`` (``'a*b'``, ``{a+b}``, ``lacc``) looking each name up
    in ``chain`` (first dict that defines it wins). Returns
    ``(value, scaled)``; raises :class:`_Unresolved`."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    elif len(text) >= 2 and text[0] == "{" and text[-1] == "}":
        text = text[1:-1]

    def lookup(name: str) -> tuple[float, bool]:
        key = name.lower()
        if key in active:
            raise _Unresolved(name)
        for candidate in chain:
            if key in candidate:
                return _resolve_value(candidate[key], chain, (*active, key))
        raise _Unresolved(name)

    evaluator = _ExprEvaluator(text, lookup)
    return evaluator.parse(), evaluator.scaled


def _resolve_params(
    params: dict[str, str], subckt_scope: str, scopes: ParamScopes
) -> dict[str, str]:
    """Replace each non-literal ``params`` value with the plain literal it
    resolves to, looking names up in the enclosing ``.subckt``'s header
    defaults / ``.param`` lines, then the top-level ``.param`` lines (a
    sibling call parameter is not a symbol, as in SPICE). A value that is
    already a plain number, or that cannot be resolved (a function call, an
    unknown name), is left as written so the consumer that actually reads
    that parameter reports it by name (see :func:`_parse_um`); parameters
    nobody reads (``ad``/``as``/...) therefore never fail the conversion."""
    chain = [scopes.get(subckt_scope, {}), scopes[""]]
    out: dict[str, str] = {}
    for key, raw in params.items():
        if spice_number.NUMBER_RE.match(raw.strip()):
            out[key] = raw
            continue
        try:
            value, scaled = _resolve_value(raw, chain)
        except _Unresolved:
            out[key] = raw
            continue
        if scaled:
            out[key] = f"{value:.12e}"
        elif value == int(value):
            out[key] = str(int(value))
        else:
            out[key] = repr(value)
    return out


#: Parameters :func:`_resolve_params` resolves but no converter reads, so a
#: call-site override of a header parameter they use is harmless.
_UNREAD_PARAMS = frozenset({"ad", "as", "pd", "ps"})

_SYMBOL_OR_NUMBER_RE = re.compile(
    r"(?P<sym>[A-Za-z_][A-Za-z0-9_]*)|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?[a-zA-Z]*"
)

#: ``{<subckt>: {<header param>: (<calling card>, <call-site value>)}}``.
CallOverrides = dict[str, dict[str, tuple[str, str]]]


def _record_call_overrides(
    card: str, current: str, scopes: ParamScopes, overrides: CallOverrides
) -> None:
    """Add to ``overrides`` each header parameter the ``X`` ``card`` (inside
    subcircuit ``current``) passes with a value other than the callee's
    default."""
    tokens = _tokenize(card)
    positional, params = _split_params(tokens[1:])
    if not positional:
        return
    callee = positional[-1].lower()
    callee_scope = scopes.get(callee)
    if callee_scope is None:
        return
    for key, value in params.items():
        if key not in callee_scope:
            continue
        try:
            same = (
                _resolve_value(value, [scopes.get(current, {}), scopes[""]])[0]
                == _resolve_value(callee_scope[key], [callee_scope, scopes[""]])[0]
            )
        except _Unresolved:
            same = value.strip().lower() == callee_scope[key].strip().lower()
        if not same:
            overrides.setdefault(callee, {}).setdefault(key, (tokens[0], value))


def _collect_call_overrides(lines: list[str], scopes: ParamScopes) -> CallOverrides:
    """Find every ``X`` call that passes a ``.subckt`` header parameter with a
    value other than the header default (issue #4406). The converter rewrites
    each device card once, in the definition, so such a value never reaches
    it; :func:`_overridden_dependency` uses this to refuse loudly."""
    overrides: CallOverrides = {}
    current = ""
    for line in lines:
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith(".subckt"):
            tokens = _tokenize(stripped)
            current = tokens[1].lower() if len(tokens) >= 2 else ""
            continue
        if lowered.startswith(".ends"):
            current = ""
            continue
        if not stripped or stripped[0] not in "Xx":
            continue
        _record_call_overrides(stripped, current, scopes, overrides)
    return overrides


def _overridden_dependency(
    raw: str,
    subckt_scope: str,
    scopes: ParamScopes,
    overrides: CallOverrides,
    seen: frozenset[str] = frozenset(),
) -> tuple[str, str, str] | None:
    """The ``(header parameter, calling card, call-site value)`` that the
    expression ``raw`` depends on and some call overrides, or ``None``."""
    scope_overrides = overrides.get(subckt_scope, {})
    for match in _SYMBOL_OR_NUMBER_RE.finditer(raw):
        name = match.group("sym")
        if name is None:
            continue
        key = name.lower()
        if key in seen:
            continue
        if key in scope_overrides:
            return (key, *scope_overrides[key])
        for scope in (scopes.get(subckt_scope, {}), scopes[""]):
            if key in scope:
                hit = _overridden_dependency(
                    scope[key], subckt_scope, scopes, overrides, seen | {key}
                )
                if hit is not None:
                    return hit
                break
    return None


def _reject_overridden_params(
    name_token: str,
    raw_params: dict[str, str],
    subckt_scope: str,
    scopes: ParamScopes | None,
    overrides: CallOverrides | None,
) -> None:
    """Raise :class:`NormalizeError` if a read parameter of device
    ``name_token`` depends on a header parameter some call overrides (a no-op
    without ``scopes`` or ``overrides``)."""
    if scopes is None or not overrides:
        return
    for key, raw in raw_params.items():
        if key in _UNREAD_PARAMS:
            continue
        hit = _overridden_dependency(raw, subckt_scope, scopes, overrides)
        if hit is not None:
            param, caller, value = hit
            raise NormalizeError(
                f"device '{name_token}': {key.upper()}={raw} in subcircuit "
                f"'{subckt_scope}' depends on header parameter "
                f"{param.upper()}, which call '{caller}' overrides to "
                f"{value}; call-site parameters are not propagated into "
                "the definition -- write the device with the literal "
                "value or give the subcircuit a single parameter value"
            )


def _split_params(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split an ``X`` card's tokens (after the instance name) into its
    leading positional tokens (nodes + subcircuit name) and its
    ``key=value`` parameter map (keys lower-cased).

    A parameter is any token containing ``=``; positional tokens never do.
    SPICE's own :data:`_PARAMS_SEPARATOR` (``PARAMS:``) keyword is neither --
    it is dropped as the pure separator it is (issue #2327), so the
    subcircuit name stays ``positional[-1]`` for the ``X ... <name> PARAMS:
    W=... L=...`` shape ``kdb.NetlistSpiceWriter`` round-trips a custom
    device class through. Before this, that card's ``positional[-1]``
    resolved to the literal string ``"PARAMS:"``.
    """
    positional: list[str] = []
    params: dict[str, str] = {}
    for token in tokens:
        if token.lower() == _PARAMS_SEPARATOR:
            continue
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
    placeholder_value_classes: dict[str, str] | None = None,
    custom_device_classes: Mapping[str, str] | None = None,
    param_scopes: ParamScopes | None = None,
    subckt_scope: str = "",
    resistor_values: Mapping[str, Callable[[float, float], float]] | None = None,
    mos_flavour_classes: bool = False,
    call_overrides: CallOverrides | None = None,
) -> str:
    """Convert one ``X`` subcircuit-call line to a plain-element ``M``/``R``/
    ``C``/``Q`` line, or return it unchanged if it is not a recognised device
    call (issue #1130 extends the original #280 MOS-only conversion to
    resistor, capacitor, and bipolar).

    ``param_scopes``/``subckt_scope`` (issue #2953) carry the netlist's
    ``.param``/``.subckt``-default symbols and the enclosing ``.subckt``'s
    name: a call-site value that is an identifier or simple expression
    (``L=lacc``, ``W='w*2'``) is resolved to a literal before any geometry is
    parsed (see :func:`_resolve_params`).

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

    ``placeholder_value_classes`` (issue #1907) is an optional collector: for
    every resistor/capacitor call converted here -- the two families whose
    plain-element card needs a positional *value* token this module has no
    PDK data to compute (see :func:`_convert_geometry_card`) -- the emitted
    device-class name is recorded against its family (``"resistor"`` /
    ``"capacitor"``), so a downstream consumer can tell that the class's
    value token is the literal ``0`` placeholder rather than a real
    resistance/capacitance. ``None`` (the default) records nothing.

    ``resistor_values`` (issue #2905) maps a lower-cased resistor device-class
    name to its deck-derived ``(L_um, W_um) -> R`` function; a resistor call
    of such a class gets its real value instead of the ``0`` placeholder and
    is not recorded in ``placeholder_value_classes``.

    ``custom_device_classes`` (issue #2327) is ``deck``'s
    :func:`~klayout_tools.netlist_capacitor_recovery.custom_device_classes_for_deck`
    table -- ``{<UPPER-CASED class name>: <canonical name>}`` for every custom
    (``kdb.GenericDeviceExtractor``-recognised) device class the deck declares,
    today exactly its ``mom_capacitors``. An ``X`` card naming one of those is
    **passed through untouched**, because such a class has no plain-element
    SPICE element letter to convert *to*: the round-tripped ``X ... PARAMS:``
    card is already the form ``klt lvs``'s own reader recognises, via
    :func:`~klayout_tools.netlist_capacitor_recovery.make_capacitor_class_recovery_reader`'s
    ``custom_device_classes`` parameter (issue #1942). Without this the card's
    carried ``W``/``L`` tripped the device-like heuristic below and
    :func:`_resolve_binding` raised -- making ``form: "subckt-call"``
    unusable for any netlist that mixes a curated device with a MoM
    capacitor. ``None``/``{}`` (the default, and every deck that declares no
    custom class) leaves behaviour byte-for-byte unchanged.
    """
    tokens = _tokenize(line)
    if not tokens:
        return line

    name_token = tokens[0]
    rest = tokens[1:]
    positional, params = _split_params(rest)
    if not positional:
        return line
    raw_params = params
    if param_scopes is not None:
        params = _resolve_params(params, subckt_scope, param_scopes)

    subckt_name = positional[-1]
    nodes = positional[:-1]

    if _passes_through_unconverted(
        subckt_name, params, subckt_to_binding, custom_device_classes
    ):
        return line

    _reject_overridden_params(
        name_token, raw_params, subckt_scope, param_scopes, call_overrides
    )

    lookup = _resolve_binding(subckt_name, subckt_to_binding)
    instance = _instance_name(name_token, lookup.kind)

    if lookup.kind == "mos":
        lookup = _flavoured_mos_lookup(lookup, subckt_name, mos_flavour_classes)
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
        card, is_placeholder = _convert_geometry_card(
            instance,
            nodes,
            subckt_name,
            lookup,
            params,
            geometry_style=geometry_style,
            resistor_value=(resistor_values or {}).get(lookup.device_class.lower()),
        )
        if is_placeholder and placeholder_value_classes is not None:
            placeholder_value_classes[lookup.device_class] = "resistor"
        return card
    if lookup.kind == "capacitor":
        card = _convert_capacitor_card(
            instance, nodes, subckt_name, lookup, params, geometry_style=geometry_style
        )
        if placeholder_value_classes is not None:
            placeholder_value_classes[lookup.device_class] = "capacitor"
        return card
    return _convert_passive_or_bipolar_card(
        instance, nodes, subckt_name, lookup, params, geometry_style=geometry_style
    )


def _convert_passive_or_bipolar_card(
    instance: str,
    nodes: list[str],
    subckt_name: str,
    lookup: DeviceLookup,
    params: dict[str, str],
    *,
    geometry_style: str | None = None,
) -> str:
    """The non-MOS/R/C tail of :func:`_convert_x_card`'s dispatch: a junction
    diode, an ``inductor``/``varactor`` call (issue #3206) or, otherwise, a
    bipolar."""
    if lookup.kind == "diode":
        return _convert_diode_card(
            instance, nodes, subckt_name, lookup, params, geometry_style=geometry_style
        )
    if lookup.kind in _PASSIVE_X_TERMINALS:
        return _convert_passive_x_card(instance, nodes, subckt_name, lookup)
    return _convert_bipolar_card(instance, nodes, subckt_name, lookup, params)


def _convert_passive_x_card(
    instance: str, nodes: list[str], subckt_name: str, lookup: DeviceLookup
) -> str:
    """Normalize an ``inductor``/``varactor`` ``device_map`` call (issue
    #3206): ``X<name> n1 n2 [n3 [n4]] <class>``.

    SPICE has no element letter for a substrate-terminal inductor or a
    4-terminal varactor (``L`` is two-terminal; there is no varactor
    element), so unlike MOS/R/C/Q the card stays a subcircuit call and only
    the model name is rewritten to the device-class label -- the form a
    reader that binds devices by model name sees. Terminal order is the call
    order, validated against the kind's terminal count so a mis-wired
    reference fails loudly instead of binding the wrong pins. Call-site
    parameters are not carried: geometry is not compared for these classes.
    """
    expected = _PASSIVE_X_TERMINALS[lookup.kind]
    if len(nodes) != expected:
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): a "
            f"device_map kind '{lookup.kind}' has {expected} terminals, found "
            f"{len(nodes)}"
        )
    return f"{instance} {' '.join(nodes)} {lookup.device_class}"


def _flavoured_mos_lookup(
    lookup: DeviceLookup, subckt_name: str, enabled: bool
) -> DeviceLookup:
    """Issue #3650: with ``enabled``, a flavoured MOS model (``nvt``, ``hvi``,
    ...) gets its own device class so the compare can tell it from the default
    one -- the layout side is split the same way. Otherwise ``lookup`` as-is."""
    flavour = mos_flavour_for_subckt(subckt_name) if enabled else None
    if flavour is None:
        return lookup
    return replace(
        lookup,
        device_class=flavoured_mos_class_name(lookup.device_class, flavour),
    )


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
    multiplier = _resolve_multiplier(instance, subckt_name, params)
    _check_mos_mult(instance, subckt_name, params)
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

    if finger_count == 1 and multiplier == 1:
        return (
            f"{instance} {' '.join(nodes)} {lookup.device_class} "
            f"L={_format_um(l_um)} W={_format_um(w_um)}"
        )
    return _expand_mos_fingers(
        instance, nodes, lookup.device_class, l_um, w_um, finger_count, multiplier
    )


def _resolve_finger_count(
    instance: str, subckt_name: str, params: dict[str, str]
) -> int:
    """Parse and validate a MOS call's ``nf`` (default ``1`` when absent),
    returning the number of physical gate fingers to expand into (issue
    #1487).

    ``nf`` describes a single real device folded into ``nf`` physical gate
    fingers -- a shape the curated plain-element MOS class cannot represent
    directly, but *can* be represented faithfully as ``nf`` separate
    unit-width plain ``M`` cards (see :func:`_expand_mos_fingers`). ``nf``
    must be a positive integer -- there is no way to fold a fractional or
    negative number of physical gate fingers, so a non-integer value is
    still a hard error (never silently rounded).
    """
    return _resolve_positive_int(
        instance,
        subckt_name,
        params,
        "nf",
        "to expand into that many parallel plain-element gate fingers",
    )


def _resolve_multiplier(instance: str, subckt_name: str, params: dict[str, str]) -> int:
    """Parse and validate a MOS call's instance multiplier ``m`` (default
    ``1`` when absent), returning the number of parallel whole-device
    copies to expand into (issue #3123).

    ``m`` is the SPICE instance multiplier (``N`` identical devices in
    parallel), which is exactly what ``N`` parallel plain ``M`` cards
    represent -- reconciled against a drawn N-unit layout by
    ``options.combine_devices``. Same positive-integer rule as ``nf``.

    ``mult`` is deliberately *not* a multiplier here: in the sky130 model
    subckt it only enters the mismatch-sigma expressions
    (``sqrt(l*w*mult)``) and is never forwarded to the device card as
    ``m``, so it does not change the nominal device and is ignored (but
    still must be numeric -- see :func:`_check_mos_mult`).
    """
    return _resolve_positive_int(
        instance,
        subckt_name,
        params,
        "m",
        "to expand into that many parallel plain-element devices",
    )


def _check_mos_mult(instance: str, subckt_name: str, params: dict[str, str]) -> None:
    """Require a MOS call's mismatch-only ``mult`` (issue #3123) to be
    numeric when present; its value never changes the converted devices."""
    if "mult" not in params:
        return
    try:
        float(params["mult"])
    except ValueError as exc:
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): "
            f"parameter 'mult' value '{params['mult']}' is not numeric"
        ) from exc


def _resolve_positive_int(
    instance: str,
    subckt_name: str,
    params: dict[str, str],
    name: str,
    purpose: str,
) -> int:
    """Shared positive-integer parse for MOS ``nf`` / ``m`` (default 1)."""
    if name not in params:
        return 1
    raw = params[name]
    try:
        value = float(raw)
    except ValueError as exc:
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): "
            f"parameter '{name}' value '{raw}' is not numeric"
        ) from exc
    if value <= 0 or value != int(value):
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): "
            f"{name}={raw} must be a positive integer {purpose}"
        )
    return int(value)


def _expand_mos_fingers(
    instance: str,
    nodes: list[str],
    device_class: str,
    l_um: float,
    w_um: float,
    finger_count: int,
    multiplier: int = 1,
) -> str:
    """Expand one folded ``nf``-finger MOS call into ``finger_count``
    parallel unit-width plain-element ``M`` cards, one line per finger,
    joined by newlines (issue #1487); an instance multiplier ``m`` (issue
    #3123) replicates that whole set ``multiplier`` times, so the result is
    ``finger_count * multiplier`` cards, each ``w_um / finger_count`` wide
    (``w_um`` is per ``m`` copy: total width is ``multiplier * w_um``).

    Deterministic naming -- ``<instance>_f0``, ``<instance>_f1``, ... (``f``
    for "finger", 0-indexed) when only ``nf`` folds; ``<instance>_m0``, ...
    (``m`` for "multiplier copy") when only ``m`` replicates; and
    ``<instance>_m0_f0``, ... when both -- so device identity is stable across runs,
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
    lines = []
    for j in range(multiplier):
        for i in range(finger_count):
            parts = []
            if multiplier > 1:
                parts.append(f"m{j}")
            if finger_count > 1:
                parts.append(f"f{i}")
            lines.append(
                f"{instance}_{'_'.join(parts)} {' '.join(nodes)} {device_class} "
                f"L={_format_um(l_um)} W={_format_um(w_finger_um)}"
            )
    return "\n".join(lines)


def _convert_geometry_card(
    instance: str,
    nodes: list[str],
    subckt_name: str,
    lookup: DeviceLookup,
    params: dict[str, str],
    *,
    geometry_style: str | None = None,
    resistor_value: Callable[[float, float], float] | None = None,
) -> tuple[str, bool]:
    """Convert a resistor call to a plain-element ``R`` card, returning
    ``(card, is_placeholder)``.

    ``resistor_value`` (issue #2905) is the deck-derived ``(L_um, W_um) -> R``
    function for this class (the same corrected formula ``klt extract`` uses
    for ``r_ohm``). When given and the call states its geometry, the real
    value replaces the placeholder ``0`` and ``is_placeholder`` is ``False``.

    KLayout's native ``DeviceClassResistor`` requires a positional value
    token before the model name (``R<name> n1 n2 [n3] <value> <model>``, the
    standard SPICE resistor shape); this module has no PDK sheet-resistance
    data to compute that value from geometry (that lives in
    ``klayout_tools.decks``, deliberately not a dependency of this module --
    see the module docstring), so it writes a placeholder ``0`` there and
    carries the call's own length/width geometry onto ``L=``/``W=`` instead
    -- ``DeviceClassResistor`` natively accepts both (confirmed against the
    installed ``klayout.db`` module), the same way the MOS path carries
    ``L=``/``W=``.

    That placeholder is *reported*, not merely written (issue #1907): the
    emitted device-class name is recorded in
    :attr:`ReferenceConversion.placeholder_value_classes`, so ``klt lvs``
    can stop ``NetlistComparer`` from comparing it as if it were a real
    resistance. Left uncorrected, a reference-side class whose every
    instance carries the same ``0`` fails to pair against the layout side's
    real, geometry-computed values *at all* -- not as a per-device
    parameter finding, but as a wholesale "device has no counterpart"
    cascade for the entire class, even when net topology alone would
    disambiguate the instances unambiguously.

    Geometry is carried only when the call actually supplies
    it (the subcircuit's own default otherwise applies); a real curated
    resistor subcircuit is legitimately 2- or 3-terminal (see
    :data:`_CAPACITOR_TERMINALS`'s docstring note), so the terminal count is
    not validated here -- nodes pass through positionally, unchanged.
    """
    _reject_multiplicity(instance, subckt_name, params)
    resolved = _resolve_call_geometry(
        instance, subckt_name, lookup, params, geometry_style=geometry_style
    )
    if resolved is None:
        extra = ""
    else:
        extra = f" L={_format_um(resolved[0])} W={_format_um(resolved[1])}"
    value = "0"
    is_placeholder = True
    if resistor_value is not None and resolved is not None and resolved[1] > 0:
        value = repr(float(resistor_value(*resolved)))
        is_placeholder = False
    return (
        f"{instance} {' '.join(nodes)} {value} {lookup.device_class}{extra}",
        is_placeholder,
    )


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

    An instance multiplier ``m`` (issue #4318, mirroring #3123 for MOS)
    expands into ``m`` parallel cards named ``<instance>_m0``, ...; ``A``/``P``
    stay per copy. ``mf``/``nf``/``mult`` greater than 1 are still rejected.
    """
    if len(nodes) != _CAPACITOR_TERMINALS:
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): expected "
            f"{_CAPACITOR_TERMINALS} terminals (A B), found {len(nodes)} "
            f"({' '.join(nodes) or '<none>'})"
        )
    multiplier = _resolve_positive_int(
        instance,
        subckt_name,
        params,
        "m",
        "to expand into that many parallel plain-element devices",
    )
    _reject_multiplicity(
        instance,
        subckt_name,
        params,
        exclude=_CAPACITOR_EXCLUDED_MULTIPLICITY_PARAMS,
    )

    has_length_param = lookup.length_param in params
    has_width_param = lookup.width_param in params
    extra = ""
    if has_length_param or has_width_param:
        l_um = _resolve_geometry_dimension(
            instance,
            subckt_name,
            lookup.length_param,
            params,
            has_length_param,
            lookup.fixed_length_um,
            geometry_style=geometry_style,
        )
        w_um = _resolve_geometry_dimension(
            instance,
            subckt_name,
            lookup.width_param,
            params,
            has_width_param,
            lookup.fixed_width_um,
            geometry_style=geometry_style,
        )
        _require_both(instance, subckt_name, lookup, l_um is not None, w_um is not None)
        area_um2 = l_um * w_um
        perimeter_um = 2.0 * (l_um + w_um)
        extra = f" A={_format_um2(area_um2)} P={_format_um(perimeter_um)}"

    if multiplier == 1:
        return f"{instance} {' '.join(nodes)} 0 {lookup.device_class}{extra}"
    # Instance multiplier ``m`` (issue #4318): ``m`` parallel copies, each
    # carrying the per-copy A/P, folded back by ``options.combine_devices``.
    return "\n".join(
        f"{instance}_m{j} {' '.join(nodes)} 0 {lookup.device_class}{extra}"
        for j in range(multiplier)
    )


def _parse_um2(
    raw_value: str,
    *,
    device: str,
    param: str,
    geometry_style: str | None = None,
) -> float:
    """Parse a SPICE area literal to square micrometres, with the same
    bare-literal convention as :func:`_parse_um` (a suffixed/exponent literal
    is SI m^2; a bare one is already um^2 under a ``BARE_UM`` deck such as
    sky130's ``.option scale=1.0u``)."""
    match = spice_number.NUMBER_RE.match(raw_value.strip())
    if match is None:
        raise NormalizeError(
            f"device '{device}': parameter '{param}' value '{raw_value}' is "
            "not a plain numeric literal"
        )
    mantissa_text, raw_suffix = match.group(1), match.group(2)
    mantissa = float(mantissa_text)
    if not raw_suffix and "e" not in mantissa_text.lower():
        if geometry_style is None:
            raise NormalizeError(
                f"device '{device}': parameter '{param}' value '{raw_value}' "
                "has no unit suffix or exponent, so it is ambiguous whether it "
                "is SI m^2 or already um^2 under an ambient '.option scale' -- "
                'pass reference.deck (e.g. "sky130") or write an explicit '
                "unit-suffixed literal (e.g. area=1p)"
            )
        if geometry_style == GEOMETRY_STYLE_BARE_UM:
            return mantissa
    return mantissa * spice_number.suffix_scale(raw_suffix) * 1e12


def _convert_diode_card(
    instance: str,
    nodes: list[str],
    subckt_name: str,
    lookup: DeviceLookup,
    params: dict[str, str],
    *,
    geometry_style: str | None = None,
) -> str:
    """Convert a PDK junction-diode call (``X<n> anode cathode <subckt>
    area=.. perim=..``, issue #4319) to a plain ``D<n> anode cathode <class>
    A=.. P=..`` card, which KLayout's reader reads as ``DeviceClassDiode``
    (terminals ``A``, ``C``; parameters ``A`` um^2, ``P`` um) -- the class the
    layout-side ``DeviceExtractorDiode`` emits. ``perim`` may be spelled
    ``pj``. An omitted parameter is left off the card (class default).
    ``mult``/``m`` > 1 is rejected, as for resistors/capacitors."""
    if len(nodes) != 2:
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): expected 2 "
            f"terminals (anode cathode), found {len(nodes)} "
            f"({' '.join(nodes) or '<none>'})"
        )
    _reject_multiplicity(instance, subckt_name, params)
    extra = ""
    if "area" in params:
        area_um2 = _parse_um2(
            params["area"],
            device=instance,
            param="area",
            geometry_style=geometry_style,
        )
        extra += f" A={_format_um2(area_um2)}"
    perim = params.get("perim", params.get("pj"))
    if perim is not None:
        perim_um = _parse_um(
            perim, device=instance, param="perim", geometry_style=geometry_style
        )
        extra += f" P={_format_um(perim_um)}"
    return f"{instance} {' '.join(nodes)} {lookup.device_class}{extra}"


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
    ``R``/``C``, no positional value token is needed at all. A four-terminal
    call (``c b e s``, issue #2862's substrate-terminal devices) converts to
    the identically-shaped four-node card ``Q<name> c b e s <model>``, which
    the same reader reads back as a ``DeviceClassBJT4Transistor`` -- the
    four nodes are carried verbatim, no PDK curated table distinguishes the
    flavours, and every rule below (nominal-``AE`` stating, ``mult`` onto
    ``NE``) applies unchanged. The real
    sky130 ``pnp_05v5`` subcircuits carry no length/width-style geometry
    parameter (fixed-geometry cells selected by name, see the module
    docstring); their only real call-site parameter is an optional
    ``mult``, carried onto the plain-element card's ``NE`` (number of
    parallel emitters), which ``DeviceClassBJT3Transistor`` natively
    supports -- so, unlike MOS/resistor/capacitor, ``mult`` here is carried,
    not rejected.

    The *emitter geometry* those cells omit at the call site is not unknown
    (issue #2335): the curated cell name encodes it, and
    :attr:`~klayout_tools.pdk_models.DeviceLookup.emitter_area_um2` carries
    the nominal emitter area ``pdk_models``' own bipolar table already
    records for it. When present it is stated on the card as
    ``AE=``/``PE=``, because ``AE`` is one of
    ``DeviceClassBJT3Transistor``'s two *primary* (compared) parameters
    (``AE``, ``NE``; confirmed against the installed ``klayout.db`` module):
    a geometry-free reference card is a zero-vs-nonzero difference against
    the layout side's extracted ``AE``, so *every* bipolar reports as
    unmatched under the default full-parameter compare unless the caller
    scopes it down with ``options.compare_parameters`` (issue #1928's
    generic escape hatch). ``PE`` is derived rather than stored -- both
    curated sky130 variants are square emitters (``W<n>L<n>`` with equal
    ``W``/``L``), so ``side = sqrt(AE)`` and ``PE = 4 * side``, which keeps
    the table's nominal area the single source of truth.

    ``AB``/``PB``/``AC``/``PC`` are deliberately *not* emitted: they measure
    drawn base/collector geometry that the fixed-geometry cell name does not
    encode, so there is nothing to state honestly (see
    :data:`~klayout_tools.pdk_models._BIPOLAR_DROPPED_PARAMS`). Leaving them
    at the class default costs nothing: they are *secondary* parameters, so
    once the primary ``AE``/``NE`` agree the comparer pairs the devices and
    never reports them (asserted end to end in ``tests/test_lvs.py``).
    """
    if len(nodes) not in (3, 4):
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): expected "
            f"3 or 4 terminals (c b e [s]), found {len(nodes)} "
            f"({' '.join(nodes) or '<none>'})"
        )

    extra = ""
    area_um2 = lookup.emitter_area_um2
    if area_um2 is not None and area_um2 > 0:
        side_um = math.sqrt(area_um2)
        extra += f" AE={_format_um2(area_um2)} PE={_format_um(4.0 * side_um)}"
    if "mult" in params:
        try:
            mult = float(params["mult"])
        except ValueError as exc:
            raise NormalizeError(
                f"device '{instance}' (subcircuit '{subckt_name}'): "
                f"parameter 'mult' value '{params['mult']}' is not numeric"
            ) from exc
        extra += f" NE={mult:g}"

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
    neither (the subcircuit's own default geometry then applies).

    A dimension the call omits still resolves when ``lookup`` carries a
    fixed constant for it (``fixed_length_um``/``fixed_width_um``, issue
    #2459's fixed-geometry-wrapper support -- see
    :func:`_resolve_geometry_dimension`) -- e.g. a call supplying only ``L``
    against a subcircuit whose *name* fixes ``W`` (sky130's
    ``res_high_po_2p85``) still emits both.
    """
    resolved = _resolve_call_geometry(
        instance, subckt_name, lookup, params, geometry_style=geometry_style
    )
    if resolved is None:
        return ""
    l_um, w_um = resolved
    return f" L={_format_um(l_um)} W={_format_um(w_um)}"


def _resolve_call_geometry(
    instance: str,
    subckt_name: str,
    lookup: DeviceLookup,
    params: dict[str, str],
    *,
    geometry_style: str | None = None,
) -> tuple[float, float] | None:
    """The call's resolved ``(L, W)`` in micrometres, or ``None`` when the
    call supplies neither dimension (the subcircuit's own default geometry
    then applies). Shared by :func:`_geometry_suffix` and the deck-derived
    resistor value (issue #2905)."""
    has_length_param = lookup.length_param in params
    has_width_param = lookup.width_param in params
    if not (has_length_param or has_width_param):
        return None
    l_um = _resolve_geometry_dimension(
        instance,
        subckt_name,
        lookup.length_param,
        params,
        has_length_param,
        lookup.fixed_length_um,
        geometry_style=geometry_style,
    )
    w_um = _resolve_geometry_dimension(
        instance,
        subckt_name,
        lookup.width_param,
        params,
        has_width_param,
        lookup.fixed_width_um,
        geometry_style=geometry_style,
    )
    _require_both(instance, subckt_name, lookup, l_um is not None, w_um is not None)
    assert l_um is not None and w_um is not None
    return l_um, w_um


def _resolve_geometry_dimension(
    instance: str,
    subckt_name: str,
    param: str | None,
    params: dict[str, str],
    has_param: bool,
    fixed_um: float | None,
    *,
    geometry_style: str | None = None,
) -> float | None:
    """Resolve one geometry dimension (length or width) for a device call
    (issue #2459): the call-site value when the call actually supplies
    ``param``; otherwise ``fixed_um`` -- the constant a fixed-geometry
    wrapper subcircuit bakes into its *name* rather than exposing as a
    call-site parameter (from a curated table entry or an explicit
    ``reference.device_map`` override, see
    :class:`~klayout_tools.pdk_models.DeviceLookup`); otherwise ``None``
    (the dimension is not resolvable at all). A call-site value always wins
    over a fixed constant when both are present -- matching a real override's
    precedence over a subcircuit default.
    """
    if has_param:
        assert param is not None  # has_param can only be True when it isn't
        return _parse_um(
            params[param],
            device=instance,
            param=param,
            geometry_style=geometry_style,
        )
    return fixed_um


def _require_both(
    instance: str,
    subckt_name: str,
    lookup: DeviceLookup,
    has_length: bool,
    has_width: bool,
) -> None:
    if has_length and has_width:
        return
    hint = ""
    if lookup.kind in ("resistor", "capacitor"):
        hint = (
            " -- if this subcircuit's name bakes in a fixed length or width "
            "instead of taking it as a call-site parameter (a PDK "
            "fixed-geometry wrapper, e.g. sky130's 'res_high_po_2p85'), pass "
            "reference.device_map's 'length_um'/'width_um' to state the "
            "constant dimension"
        )
    raise NormalizeError(
        f"device '{instance}' (subcircuit '{subckt_name}'): both "
        f"'{(lookup.length_param or '').upper()}' and "
        f"'{(lookup.width_param or '').upper()}' must be given together{hint}"
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
    call site passes ``_MOS_EXCLUDED_MULTIPLICITY_PARAMS``
    because :func:`_convert_mos_card` already resolved and validated
    ``nf``/``m``/``mult`` itself (expanded or ignored, not rejected);
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


def _passes_through_unconverted(
    subckt_name: str,
    params: dict[str, str],
    subckt_to_binding: dict[str, DeviceLookup] | None,
    custom_device_classes: Mapping[str, str] | None = None,
) -> bool:
    """Whether :func:`_convert_x_card` must leave this ``X`` card exactly as
    written -- the passthrough-vs-convert-or-raise decision, factored out of
    that function so each of its three (independent) reasons stays readable.

    A device call is recognised either by its subcircuit name resolving
    against the curated table (covers every family, including bipolar, which
    carries no geometry-style parameter at all -- see the module docstring),
    or, when the name does not resolve, by carrying a geometry-style
    parameter that *looks* like a device call gone wrong (the original #280
    MOS-only heuristic, now shared with resistor/capacitor). Neither signal
    present means a genuine hierarchical subcircuit instance -- passed
    through untouched.

    Between those two, a name matching one of ``custom_device_classes``
    (issue #2327) also passes through: a custom
    (``kdb.GenericDeviceExtractor``-recognised) device class of the requested
    deck is a *real* device, but one with no plain-element form to convert
    to -- its round-tripped ``X ... PARAMS:`` card is already what the
    downstream reader wants (see :func:`_convert_x_card`'s own docstring).
    Checked only for a name the curated table did **not** already bind, so a
    curated binding always wins.

    A ``False`` here does not promise the card converts -- a device-like
    name that resolves in neither table is exactly the case
    :func:`_resolve_binding` raises on, deliberately.
    """
    if _binding_known(subckt_name, subckt_to_binding):
        return False
    if custom_device_classes and subckt_name.upper() in custom_device_classes:
        return True
    return not any(key in params for key in _DEVICE_LIKE_PARAMS)


class _UnknownSubcircuitError(NormalizeError):
    """A device-like ``X`` card whose subcircuit resolves in no table.

    Carries the pieces of its message so :func:`convert_reference_netlist`
    can keep converting, collect every such name, and refuse once listing all
    of them (issue #3206) -- a caller fixing ``device_map`` one name per run
    would otherwise need as many runs as there are unknown names. A single
    unknown name still raises the original message byte-for-byte.
    """

    def __init__(self, subckt_name: str, singular: str, plural: str, tail: str) -> None:
        super().__init__(f"subcircuit '{subckt_name}' {singular} {tail}")
        self.subckt_name = subckt_name
        self.plural = plural
        self.tail = tail


def _aggregate_unknown_subcircuits(
    unknown: list[_UnknownSubcircuitError],
) -> NormalizeError:
    """One refusal for ``unknown`` (non-empty, first-seen order, de-duplicated
    by name): the original error for one name, a combined one for several."""
    unique: dict[str, _UnknownSubcircuitError] = {}
    for err in unknown:
        unique.setdefault(err.subckt_name, err)
    if len(unique) == 1:
        return next(iter(unique.values()))
    first = next(iter(unique.values()))
    names = ", ".join(f"'{name}'" for name in unique)
    return NormalizeError(f"subcircuits {names} {first.plural} {first.tail}")


def _resolve_binding(
    subckt_name: str, subckt_to_binding: dict[str, DeviceLookup] | None
) -> DeviceLookup:
    if subckt_to_binding is not None:
        lookup = subckt_to_binding.get(subckt_name)
        if lookup is None:
            available = ", ".join(sorted(subckt_to_binding)) or "<none>"
            raise _UnknownSubcircuitError(
                subckt_name,
                "is not a known device for the requested deck",
                "are not known devices for the requested deck",
                f"(known: {available}); if it is a real device, "
                "pass reference.device_map to map it explicitly",
            )
        return lookup

    known = known_device_subckt_names()
    entry = known.get(subckt_name)
    if entry is None:
        available = ", ".join(sorted(known)) or "<none>"
        raise _UnknownSubcircuitError(
            subckt_name,
            "is not a known curated PDK device",
            "are not known curated PDK devices",
            f"(known: {available}); pass reference.deck or "
            "reference.device_map to map it explicitly",
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
    if kind in _PASSIVE_X_TERMINALS:
        return name_token  # stays a subcircuit-call card: no element letter
    letter = {
        "mos": "M",
        "resistor": "R",
        "capacitor": "C",
        "bipolar": "Q",
        "diode": "D",
    }[kind]
    rest = name_token[1:]  # drop the leading X/x
    if not rest:
        return name_token  # malformed; leave as-is for the reader to reject
    if rest[0].upper() == letter:
        return rest
    return f"{letter}{rest}"


#: Every ``kind`` an object-valued ``device_map`` entry may name (issue
#: #1271) -- the same four families :class:`DeviceLookup` already supports.
_DEVICE_MAP_KINDS = ("mos", "resistor", "capacitor", "bipolar", "inductor", "varactor")

#: Terminal count of each ``device_map`` kind with no native SPICE element
#: letter (issue #3206): a spiral inductor with its substrate terminal
#: (``p1 p2 sub``) and a varactor (``p1 p2 well sub``).
_PASSIVE_X_TERMINALS = {"inductor": 3, "varactor": 4}


def _device_lookup_from_override(name: str, value: object) -> DeviceLookup:
    """Build one ``device_map`` entry's :class:`DeviceLookup` from its raw
    JSON-decoded value (issue #1271).

    Two shapes are accepted:

    - A bare string (the original #280 shape, e.g. ``"nfet"``) always means a
      4-terminal MOS ``l``/``w`` binding -- **unchanged** from before this
      issue, for full backward compatibility with every existing caller's
      ``device_map``.
    - An object (``{"kind": ..., "class": ..., "length_param": ...,
      "width_param": ..., "length_um": ..., "width_um": ...}``) opts into an
      explicit, non-MOS-only binding. ``kind`` (one of
      :data:`_DEVICE_MAP_KINDS`) and ``class`` (the plain-element
      device-class label, e.g. ``"res_generic_po"``) are required.
      ``length_param``/``width_param`` (the real subcircuit's own call-site
      geometry parameter spellings, e.g. gf180mcu's ``"r_length"``/
      ``"r_width"``) default to ``"l"``/``"w"`` and are ignored for
      ``kind: "bipolar"`` (no geometry call-site parameter at all -- see
      ``pdk_models.py``'s bipolar section); for ``kind: "mos"`` they are
      likewise ignored, since :func:`_convert_mos_card` always reads the
      literal ``l``/``w`` parameter keys regardless of the resolved
      binding's own ``length_param``/``width_param`` (matching every
      curated MOS binding, which also always carries ``"l"``/``"w"``).

    ``length_um``/``width_um`` (issue #2459, ``kind: "resistor"``/
    ``"capacitor"`` only) name the constant micrometre value a
    fixed-geometry wrapper subcircuit bakes into its *name* rather than
    exposing as a call-site parameter -- e.g. sky130's
    ``sky130_fd_pr__res_high_po_0p35`` fixes ``w=0.35`` and only accepts
    ``l`` at the call site: ``{"kind": "resistor", "class": "res_high_po",
    "width_um": 0.35}``. A call-site value for that same dimension still
    wins when the reference netlist happens to supply one (see
    :func:`_resolve_geometry_dimension`) -- the fixed value only fills the
    dimension the call omits. Omitted (the default, ``None``) means both
    dimensions must come from the call site exactly as before this issue.

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
    if kind in ("mos", "bipolar", "inductor", "varactor"):
        if "length_um" in value or "width_um" in value:
            raise NormalizeError(
                f"device_map entry '{name}': 'length_um'/'width_um' are only "
                f"supported for kind 'resistor'/'capacitor' -- '{kind}' has "
                "no call-site length/width parameter for a fixed constant "
                "to fill in for"
            )
        # mos: `_convert_mos_card` always reads literal `l`/`w`, so a custom
        # spelling here would be silently ignored -- not accepted, to avoid
        # that trap. bipolar/inductor/varactor: no geometry call-site
        # parameter is carried at all.
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
    fixed_length_um = _parse_device_map_fixed_um(name, value, "length_um")
    fixed_width_um = _parse_device_map_fixed_um(name, value, "width_um")
    return DeviceLookup(
        kind,
        device_class,
        length_param,
        width_param,
        fixed_length_um=fixed_length_um,
        fixed_width_um=fixed_width_um,
    )


def _parse_device_map_fixed_um(
    name: str, value: dict[str, object], key: str
) -> float | None:
    """Parse one ``device_map`` object entry's optional ``"length_um"``/
    ``"width_um"`` field (issue #2459) into a positive micrometre float, or
    ``None`` when the key is absent (the common case -- no fixed dimension).
    Rejects a non-numeric or non-positive value with a named error, the same
    discipline as every other object-entry field here.
    """
    if key not in value:
        return None
    raw = value[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise NormalizeError(
            f"device_map entry '{name}': '{key}' must be a number (the "
            "fixed dimension in micrometres this subcircuit's name bakes "
            f"in), found {type(raw).__name__}"
        )
    if raw <= 0:
        raise NormalizeError(
            f"device_map entry '{name}': '{key}' must be a positive number, "
            f"found {raw!r}"
        )
    return float(raw)


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


def _resolve_custom_device_classes(deck: str | None) -> dict[str, str]:
    """``deck``'s custom (``kdb.GenericDeviceExtractor``-recognised) device
    classes as ``{<UPPER-CASED class name>: <canonical name>}`` -- the table
    :func:`_convert_x_card` passes a round-tripped ``X ... PARAMS:`` card
    through on (issue #2327), ``{}`` when no ``deck`` was given.

    Resolved exactly the way ``lvs.py``'s own ``_resolve_layout`` /
    ``_read_reference_netlist`` already resolve it for the *reading* side
    (:func:`~klayout_tools.decks.get_extraction_deck` +
    :func:`~klayout_tools.netlist_capacitor_recovery.custom_device_classes_for_deck`)
    -- never a second, independent table -- so a card this module passes
    through is exactly a card that reader recognises as a device.

    Best-effort by design: an unresolvable ``deck`` name is swallowed here
    and yields ``{}``, because :func:`_build_subckt_map` already owns raising
    the authoritative :class:`NormalizeError` for a bad ``deck`` (the same
    division of labour ``_read_reference_netlist`` documents). Imported
    lazily -- ``klayout_tools.decks`` pulls in the extraction stack, which
    this otherwise dependency-light text transform should not require at
    import time.
    """
    if not deck:
        return {}
    from .decks import (
        InvalidDeckOptionError,
        UnknownExtractionDeckError,
        get_extraction_deck,
    )
    from .netlist_capacitor_recovery import custom_device_classes_for_deck

    try:
        resolved = get_extraction_deck(deck)
    except (UnknownExtractionDeckError, InvalidDeckOptionError):
        return {}
    return custom_device_classes_for_deck(resolved)


def _deck_resistor_values(
    deck: str | None,
    classes: Collection[str] | None,
    deck_options: Mapping[str, str] | None,
) -> dict[str, Callable[[float, float], float]]:
    """``{<lower-cased class>: (L_um, W_um) -> R}`` for each requested class
    ``deck`` models with a sheet resistance (issue #2905), built on
    ``extract.resistor_r_ohm_for_geometry`` so the reference side can never
    drift from ``klt extract``'s ``r_ohm``. Best-effort: an unresolvable deck
    yields ``{}`` (:func:`_build_subckt_map` owns the authoritative error)."""
    if not deck or not classes:
        return {}
    from .decks import (
        InvalidDeckOptionError,
        UnknownExtractionDeckError,
        get_extraction_deck,
    )
    from .extract import resistor_r_ohm_for_geometry

    try:
        resolved = get_extraction_deck(deck, deck_options)
    except (UnknownExtractionDeckError, InvalidDeckOptionError):
        return {}
    wanted = {name.lower() for name in classes}
    values: dict[str, Callable[[float, float], float]] = {}
    for resistor in resolved.resistors:
        key = resistor.name.lower()
        if key in wanted and resistor.sheet_rho_ohm_sq:

            def _value(l_um: float, w_um: float, _resistor: Any = resistor) -> float:
                return resistor_r_ohm_for_geometry(_resistor, l_um, w_um)

            values[key] = _value
    return values


class ReferenceConversion(NamedTuple):
    """The result of one :func:`convert_reference_netlist` run.

    ``text`` is the converted, plain-element-form SPICE source.

    ``placeholder_value_classes`` (issue #1907) maps each emitted
    resistor/capacitor device-class name to its family (``"resistor"`` /
    ``"capacitor"``). Those are exactly the classes whose plain-element card
    carries the literal ``0`` *placeholder* in the positional value slot,
    because this module has no PDK sheet-resistance / capacitance-per-area
    table to compute a real value from (see :func:`_convert_geometry_card`
    and the module docstring). ``klt lvs`` consumes this to keep that
    placeholder from being compared as if it were a real value -- an
    all-``0`` reference-side class otherwise defeats
    ``NetlistComparer``'s device pairing for the whole class, collapsing a
    topologically-unambiguous compare into a wholesale "device has no
    counterpart" cascade. Empty when the conversion emitted no
    resistor/capacitor card at all.
    """

    text: str
    placeholder_value_classes: dict[str, str]


def _sim_model_classes(deck: str | None) -> dict[str, str]:
    """``deck``'s lower-cased simulation-model name -> device class map, used
    to rewrite plain ``M`` cards that name the PDK model (issue #3882).
    Empty without ``deck`` or for a deck with no curated MOS table."""
    if deck is None:
        return {}
    try:
        table = build_subckt_to_class_map(deck)
    except ModelBindingError:
        return {}
    return {name.lower(): device_class for name, device_class in table.items()}


def _rewrite_m_card_model(line: str, sim_model_classes: dict[str, str]) -> str:
    """Rewrite a plain ``M`` card's model token (the last positional token,
    ``M<name> d g s b <model> [k=v ...]``) to the deck's device class when it
    names a known simulation model; any other card is returned unchanged."""
    tokens = _tokenize(line.strip())
    positional, _ = _split_params(tokens)
    if len(positional) < 6:
        return line
    device_class = sim_model_classes.get(positional[-1].lower())
    if device_class is None:
        return line
    model_index = len(positional) - 1
    if tokens[model_index] != positional[-1]:
        return line
    tokens[model_index] = device_class
    return " ".join(tokens)


def _deck_plain_resistor_classes(deck: str | None) -> dict[str, str]:
    """``deck``'s lower-cased resistor model name -> canonical device class,
    used to give a valueless foundry-style ``R`` card its value token (issue
    #4670). Empty without ``deck`` or for an unresolvable deck."""
    if deck is None:
        return {}
    from .decks import (
        InvalidDeckOptionError,
        UnknownExtractionDeckError,
        get_extraction_deck,
    )
    from .netlist_capacitor_recovery import resistor_classes_for_deck

    try:
        resolved = get_extraction_deck(deck)
    except (UnknownExtractionDeckError, InvalidDeckOptionError):
        return {}
    return {
        name.lower(): canonical
        for name, canonical in resistor_classes_for_deck(resolved).items()
    }


def _is_r_value_token(token: str) -> bool:
    """Whether ``token`` is an ``R`` card's value (a SPICE number or a
    ``{expr}``/``'expr'`` expression) rather than a third terminal. The bare
    ``0`` stays a node name: it is ground, a plausible bulk terminal."""
    if token == "0":
        return False
    return token[:1] in "{'" or spice_number.parse(token) is not None


def _convert_plain_r_card(
    line: str,
    resistor_classes: dict[str, str],
    geometry_style: str | None,
    resistor_values: dict[str, Callable[[float, float], float]],
    placeholder_value_classes: dict[str, str],
) -> str:
    """Give a foundry-style ``R<name> n1 n2 [n3] <model> W=.. L=..`` card (no
    value token) the ``R... <value> <class> L= W=`` shape KLayout's reader
    needs, through the same value/placeholder logic as the ``X``-call route
    (issue #4670). A card with a value token, or whose model names no deck
    resistor class, is returned unchanged."""
    tokens = _tokenize(line.strip())
    positional, params = _split_params(tokens)
    # name + 2 or 3 terminals + model
    if len(positional) not in (4, 5):
        return line
    if len(positional) == 5 and _is_r_value_token(positional[3]):
        # `R<name> n1 n2 <value> <model>`: a valued card, left byte-identical
        return line
    device_class = resistor_classes.get(positional[-1].lower())
    if device_class is None:
        return line
    lookup = DeviceLookup("resistor", device_class, "l", "w")
    card, is_placeholder = _convert_geometry_card(
        positional[0],
        positional[1:-1],
        positional[-1],
        lookup,
        params,
        geometry_style=geometry_style,
        resistor_value=resistor_values.get(device_class.lower()),
    )
    if is_placeholder:
        placeholder_value_classes[device_class] = "resistor"
    return card


def convert_reference_netlist(
    text: str,
    *,
    deck: str | None = None,
    device_map: dict[str, object] | None = None,
    resistor_value_classes: Collection[str] | None = None,
    deck_options: Mapping[str, str] | None = None,
    mos_flavour_classes: bool = False,
) -> ReferenceConversion:
    """Convert subckt-call-form SPICE ``text`` to the plain-element form,
    returning the converted text *plus* the conversion metadata
    :class:`ReferenceConversion` documents.

    :func:`normalize_reference_netlist` is the text-only wrapper around this
    -- every behaviour below is shared, and described there.

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

    ``resistor_value_classes`` (issue #2905) names resistor device classes
    (case-insensitive) whose ``R`` should be computed from ``deck``'s own
    sheet resistance -- through the very function ``klt extract`` uses for
    ``r_ohm`` (fixed offset, end term and width offset included) -- and the
    call's ``L``/``W``, instead of the ``0`` placeholder. A class the deck has
    no resistor model for (or without a sheet resistance), or a call that
    states no geometry, keeps the placeholder. ``deck_options`` selects the
    deck's flavours exactly as for extraction.

    Non-device lines (comments, ``.subckt``/``.ends``/``.model``, existing
    plain-element ``M`` cards, genuine hierarchical ``X`` instances) pass
    through unchanged, so a netlist that *mixes* plain-element and subckt-call
    device lines converts correctly. Raises :class:`NormalizeError` for any
    device card that cannot be converted correctly and unambiguously.

    An ``X`` card naming one of ``deck``'s *custom*
    (``kdb.GenericDeviceExtractor``-recognised) device classes -- today its
    ``mom_capacitors``, e.g. IHP's ``cap_cmomi`` -- likewise passes through
    untouched (issue #2327), ``PARAMS:`` separator and all: that card is
    already the form ``klt lvs``'s own reader reads as a device (issue
    #1942), and the class has no plain-element element letter to convert to.
    So one ``form: "subckt-call"`` request can carry both device families of
    a mixed netlist, instead of failing on the MoM card.

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

    ``mos_flavour_classes`` (issue #3650) emits a MOS call of a flavoured
    model (e.g. sky130's ``nfet_05v0_nvt``) under its own device class
    (``nfet__nvt``) instead of the base ``nfet``, so a flavour-aware compare
    can tell it from a default-flavour device. ``False`` (the default) leaves
    the output byte-identical.
    """
    subckt_to_binding, device_map_names = _build_subckt_map(deck, device_map)
    geometry_style = geometry_style_for_family(deck) if deck is not None else None
    custom_device_classes = _resolve_custom_device_classes(deck)
    sim_model_classes = _sim_model_classes(deck)
    plain_resistor_classes = _deck_plain_resistor_classes(deck)
    logical_lines = _fold_spice_continuations(text.splitlines())
    param_scopes = _collect_param_scopes(logical_lines)
    call_overrides = _collect_call_overrides(logical_lines, param_scopes)
    subckt_scope = ""

    resistor_values = _deck_resistor_values(deck, resistor_value_classes, deck_options)
    placeholder_value_classes: dict[str, str] = {}
    out: list[str] = []
    unknown_subckts: list[_UnknownSubcircuitError] = []
    for line in logical_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            out.append(line)
            continue
        lowered = stripped.lower()
        if lowered.startswith(".subckt"):
            tokens = _tokenize(stripped)
            subckt_scope = tokens[1].lower() if len(tokens) >= 2 else ""
        elif lowered.startswith(".ends"):
            subckt_scope = ""
        first = stripped.split(None, 1)[0]
        if first and first[0] in "Xx":
            try:
                out.append(
                    _convert_x_card(
                        line,
                        subckt_to_binding,
                        device_map_names,
                        geometry_style,
                        placeholder_value_classes,
                        custom_device_classes,
                        param_scopes,
                        subckt_scope,
                        resistor_values,
                        mos_flavour_classes,
                        call_overrides,
                    )
                )
            except _UnknownSubcircuitError as exc:
                unknown_subckts.append(exc)
                out.append(line)
        elif sim_model_classes and first and first[0] in "Mm":
            out.append(_rewrite_m_card_model(line, sim_model_classes))
        elif plain_resistor_classes and first and first[0] in "Rr":
            out.append(
                _convert_plain_r_card(
                    line,
                    plain_resistor_classes,
                    geometry_style,
                    resistor_values,
                    placeholder_value_classes,
                )
            )
        else:
            out.append(line)
    if unknown_subckts:
        raise _aggregate_unknown_subcircuits(unknown_subckts)
    return ReferenceConversion("\n".join(out) + "\n", placeholder_value_classes)


def normalize_reference_netlist(
    text: str,
    *,
    deck: str | None = None,
    device_map: dict[str, object] | None = None,
) -> str:
    """Convert subckt-call-form SPICE ``text`` to the plain-element form.

    The text-only view of :func:`convert_reference_netlist` -- identical
    behaviour, for callers that do not need the conversion metadata.
    """
    return convert_reference_netlist(text, deck=deck, device_map=device_map).text


def defined_subckt_names(text: str) -> set[str]:
    """The upper-cased name of every ``.subckt`` defined in SPICE ``text``
    (issue #2952) -- upper-cased because ``NetlistSpiceReader`` reads circuit
    names back that way, so a caller can compare against
    ``kdb.Circuit.name`` directly. ``.include``-d files are not followed."""
    defined: set[str] = set()
    for line in _fold_spice_continuations(text.splitlines()):
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == ".subckt":
            defined.add(parts[1].upper())
    return defined


_INCLUDE_LINE_RE = re.compile(r"^(\s*\.(?:include|inc)\s+)(\S.*?)\s*$", re.IGNORECASE)


def quote_spice_includes(text: str, base_dir: str) -> str:
    """Single-quote the path of every unquoted ``.include``/``.inc`` line.

    KLayout's SPICE reader parses an unquoted include path as an expression, so
    a ``-`` in any component truncates it (``/tmp/a-b/c.spice`` -> ``/tmp//a``).
    Relative paths are joined to ``base_dir`` (the reference file's directory)
    so the rewritten text can be read from a temp file elsewhere. Already-
    quoted lines are left untouched; returns ``text`` itself when unchanged.
    """
    import os

    changed = False
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped) :]
        match = _INCLUDE_LINE_RE.match(stripped)
        if match and match.group(2)[0] not in "'\"":
            target = os.path.normpath(os.path.join(base_dir, match.group(2)))
            if "'" not in target:
                line = f"{match.group(1)}'{target}'{ending}"
                changed = True
        out.append(line)
    return "".join(out) if changed else text


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

    A card whose instance name contains ``$`` (``X$1``, the name KLayout's
    own extractor writes) is reported only when it is a MOS call: bare
    ``klt extract`` round-trips resistors/capacitors/bipolars as such cards
    under the same curated names (issues #1157/#1942), so those are not
    evidence of the simulation form. Only ``klt extract --pdk`` emits MOS
    ``X`` cards (issue #2972).
    """
    known = known_device_subckt_names()
    defined: set[str] = set()
    used: list[str] = []
    seen: set[str] = set()

    for line in _fold_spice_continuations(text.splitlines()):
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
        if subckt_name not in known or subckt_name in seen:
            continue
        if "$" in tokens[0] and known[subckt_name][1].kind != "mos":
            continue
        used.append(subckt_name)
        seen.add(subckt_name)

    return [name for name in used if name not in defined]
