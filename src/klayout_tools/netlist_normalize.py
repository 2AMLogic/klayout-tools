"""Convert a PDK schematic flow's *simulation* SPICE (the subcircuit-call
form) into the *schematic-equivalent, plain-element* form ``klt lvs`` requires
on its reference side (issue #280).

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

- Only ``X`` cards that *look like a MOS device* (carry an ``l``/``w``
  parameter) are candidates for conversion; any other ``X`` card (a genuine
  hierarchical subcircuit instance) passes through untouched.
- A device-like ``X`` card whose subcircuit name is **not** in the resolved
  device map is a hard error (:class:`NormalizeError`), never a silent
  pass-through -- an unrecognised device name is exactly the confusing-failure
  case this module exists to replace.
- ``L``/``W`` are carried, converted to explicit micrometre-suffixed literals
  (``0.5u`` -> ``L=0.5U``; SI metres ``1.5e-6`` -> ``W=1.5U``). Every other
  parameter is dropped -- ``ad``/``as``/``pd``/``ps``/``nrd``/``nrs``/``sa``/
  ``sb``/``sd`` (parasitic-only, not carried by ``klt extract`` either) and any
  other model parameter -- matching the plain-element form's geometric-only
  scope.
- ``nf``/``m`` > 1 (a multi-finger / multiplied device the curated plain-element
  decks cannot represent) is **rejected** with a specific error naming the
  device and value, never silently dropped or misinterpreted.

Text-level (not KLayout-object-level) on purpose: the input is *not* readable
by ``NetlistSpiceReader`` in the first place (that is the whole problem), so
there is no netlist object to rewrite -- the transform operates on the SPICE
source and hands a plain-element source back to the reader.
"""

from __future__ import annotations

import re

from .pdk_models import (
    ModelBindingError,
    _format_um,
    build_subckt_to_class_map,
    known_mos_subckt_names,
)

#: Subcircuit-call parameters carried onto the plain-element ``M`` card.
_CARRIED_PARAMS = ("l", "w")

#: Parameters that select a multi-finger / multiplied device the curated
#: plain-element decks cannot represent -- rejected (not dropped) when > 1.
_MULTIPLICITY_PARAMS = ("nf", "m", "mult")

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


def _parse_um(raw_value: str, *, device: str, param: str) -> float:
    """Parse a SPICE ``L``/``W`` literal (``0.5u``, ``1.5e-6``, ``500n``) to
    micrometres, raising :class:`NormalizeError` for anything that is not a
    plain number (e.g. a ``'...'`` expression the plain-element form cannot
    carry).

    The base unit for a MOS ``W``/``L`` is metres per the SPICE standard, so
    a bare number is metres and an engineering suffix multiplies it; the
    result is scaled to micrometres. A ``.option scale`` bare-micrometre
    convention is *not* inferred (it cannot be told apart from SI metres
    without side information) -- callers whose flow uses it must emit explicit
    unit suffixes, the same requirement ``docs/cli/lvs.md`` already documents.
    """
    match = _NUMBER_RE.match(raw_value.strip())
    if match is None:
        raise NormalizeError(
            f"device '{device}': parameter '{param.upper()}' value "
            f"'{raw_value}' is not a plain numeric literal -- the "
            "plain-element form cannot carry an expression; write an explicit "
            "geometric value (e.g. L=0.5u)"
        )
    mantissa = float(match.group(1))
    suffix = match.group(2).lower()
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


def _convert_x_card(line: str, subckt_to_class: dict[str, str] | None) -> str:
    """Convert one ``X`` subcircuit-call line to a plain-element ``M`` line,
    or return it unchanged if it is not a MOS device call.

    ``subckt_to_class`` maps a device subcircuit name to its curated
    device-class label. When ``None`` (no deck / no explicit map given), the
    device class is resolved per-name against the whole curated table.
    """
    tokens = _tokenize(line)
    if not tokens:
        return line

    name_token = tokens[0]
    rest = tokens[1:]
    positional, params = _split_params(rest)

    # A MOS device call is identified by carrying an l/w parameter. Anything
    # else is a genuine hierarchical subcircuit instance -- pass it through so
    # the reader treats it as a subcircuit, unchanged.
    if not any(key in params for key in _CARRIED_PARAMS):
        return line
    if not positional:
        return line

    subckt_name = positional[-1]
    nodes = positional[:-1]

    device_class = _resolve_device_class(subckt_name, subckt_to_class)
    instance = _instance_name(name_token)

    if len(nodes) != _MOS_TERMINALS:
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): expected "
            f"{_MOS_TERMINALS} terminals (d g s b), found {len(nodes)} "
            f"({' '.join(nodes) or '<none>'})"
        )

    for param in _MULTIPLICITY_PARAMS:
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

    l_um = _parse_um(params["l"], device=instance, param="l")
    w_um = _parse_um(params["w"], device=instance, param="w")

    return (
        f"{instance} {' '.join(nodes)} {device_class} "
        f"L={_format_um(l_um)} W={_format_um(w_um)}"
    )


def _resolve_device_class(
    subckt_name: str, subckt_to_class: dict[str, str] | None
) -> str:
    if subckt_to_class is not None:
        device_class = subckt_to_class.get(subckt_name)
        if device_class is None:
            available = ", ".join(sorted(subckt_to_class)) or "<none>"
            raise NormalizeError(
                f"subcircuit '{subckt_name}' is not a known device for the "
                f"requested deck (known: {available}); if it is a real device, "
                "pass reference.device_map to map it explicitly"
            )
        return device_class

    known = known_mos_subckt_names()
    entry = known.get(subckt_name)
    if entry is None:
        available = ", ".join(sorted(known)) or "<none>"
        raise NormalizeError(
            f"subcircuit '{subckt_name}' is not a known curated PDK device "
            f"(known: {available}); pass reference.deck or "
            "reference.device_map to map it explicitly"
        )
    return entry[1]


def _instance_name(name_token: str) -> str:
    """Turn a subckt-call instance token into a plain MOS element name.

    Idiomatically a MOS instance is emitted as ``XM1`` -- the ``X``
    subcircuit-call letter plus the ``M1`` device name -- so dropping the
    leading ``X`` recovers the natural ``M1``. When the remainder is not
    already an ``M``-element name (e.g. ``X5``), an ``M`` is prepended
    (``M5``) so the result is always a valid MOS element line.
    """
    rest = name_token[1:]  # drop the leading X/x
    if not rest:
        return name_token  # malformed; leave as-is for the reader to reject
    if rest[0] in "Mm":
        return rest
    return f"M{rest}"


def _build_subckt_map(
    deck: str | None, device_map: dict[str, str] | None
) -> dict[str, str] | None:
    """Resolve the ``<subckt-name> -> <device-class>`` map for a conversion
    request from an optional deck name and/or an optional explicit override
    map. Returns ``None`` when neither is given (per-name auto-resolution
    against the whole curated table).
    """
    resolved: dict[str, str] = {}
    if deck is not None:
        try:
            resolved.update(build_subckt_to_class_map(deck))
        except ModelBindingError as exc:
            raise NormalizeError(str(exc)) from exc
    if device_map:
        resolved.update(device_map)
    return resolved or None


def normalize_reference_netlist(
    text: str,
    *,
    deck: str | None = None,
    device_map: dict[str, str] | None = None,
) -> str:
    """Convert subckt-call-form SPICE ``text`` to the plain-element form.

    ``deck`` selects the curated device map (``"sky130"``/``"gf180mcu"``);
    ``device_map`` is an explicit ``<subckt-name> -> <device-class>`` override
    (merged on top of the deck's map). With neither, each device subcircuit
    name is auto-resolved against the whole curated table.

    Non-device lines (comments, ``.subckt``/``.ends``/``.model``, existing
    plain-element ``M`` cards, genuine hierarchical ``X`` instances) pass
    through unchanged, so a netlist that *mixes* plain-element and subckt-call
    device lines converts correctly. Raises :class:`NormalizeError` for any
    device card that cannot be converted correctly and unambiguously.
    """
    subckt_to_class = _build_subckt_map(deck, device_map)
    logical_lines = _merge_continuations(text.splitlines())

    out: list[str] = []
    for line in logical_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            out.append(line)
            continue
        first = stripped.split(None, 1)[0]
        if first and first[0] in "Xx":
            out.append(_convert_x_card(line, subckt_to_class))
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
    """
    known = known_mos_subckt_names()
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
